"""Watcher definitions and a durable job outbox per board, executed by DBOS.

Board edits (CLI and TUI saves) only use the standard library here: they
commit matching jobs to an outbox table and wake a detached worker. The
worker (``kbtd.watch_worker``) moves each job into a DBOS durable workflow in
the same SQLite file and runs it. Commands that inspect or retry jobs import
DBOS on demand. See docs/adr/0002-dbos-watcher-runtime.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbos import DBOSClient

    from .events import Event

# DBOS identifiers shared by the outbox, the worker and the inspection commands.
APP_NAME = "kbtd"
# Bump when the steps of the watcher workflow change, so that a newer worker
# never replays a recorded run with a different step sequence.
APP_VERSION = "1"
QUEUE = "kbtd-watchers"
WORKFLOW = "kbtd.watcher_action"
STEP = "kbtd.run_command"
# Automatic reruns after the worker stops during an action (at most 3 starts).
MAX_RECOVERIES = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kbtd_watchers (
    name TEXT PRIMARY KEY, trigger TEXT NOT NULL, command TEXT NOT NULL
);
-- AUTOINCREMENT never reuses a number, so each job keeps a unique DBOS workflow ID.
CREATE TABLE IF NOT EXISTS kbtd_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, trigger TEXT NOT NULL,
    command TEXT NOT NULL, event TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""
_STATUS = {
    "ENQUEUED": "queued", "DELAYED": "queued", "PENDING": "running", "SUCCESS": "succeeded",
    "ERROR": "failed", "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "interrupted", "CANCELLED": "cancelled",
}
_RETRYABLE = {"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"}
_CANCELLABLE = {"ENQUEUED", "DELAYED"}
_LOG_LIMIT = 1 << 20
_NOT_RETRYABLE = "only failed, interrupted or cancelled watcher jobs can be retried"


class SkippedWatchers(ValueError):
    """Raised after dispatch when stored watchers have triggers that no longer compile."""

    def __init__(self, names: list[str]):
        self.names = names
        listed = ", ".join(repr(name) for name in names)
        super().__init__(f"Skipped watcher {listed}: invalid trigger. Run 'kbtd watch list' to see why.")


class Watchers:
    def __init__(self, board_path: Path):
        self.board = board_path.expanduser().resolve()
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        if not root.is_absolute():
            root = Path.home() / ".local/state"
        key = hashlib.sha256(os.fsencode(self.board)).hexdigest()
        self.path = root / "kbtd" / "watchers" / f"{key}.sqlite3"
        self.log_path = self.path.with_suffix(".log")

    @property
    def url(self) -> str:
        """The DBOS system database URL; kbtd's own tables share this file."""
        return f"sqlite:///{self.path}"

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, isolation_level="IMMEDIATE", timeout=30)) as db:
                db.row_factory = sqlite3.Row
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "watchers" in tables and "kbtd_watchers" not in tables:
                    # Definitions from the version that preceded DBOS.
                    db.execute("ALTER TABLE watchers RENAME TO kbtd_watchers")
                db.executescript(_SCHEMA)
                with db:
                    yield db
        except sqlite3.Error as exc:
            raise RuntimeError(f"watcher database {self.path}: {exc}") from exc

    @contextmanager
    def _dbos_errors(self) -> Iterator[None]:
        # Report DBOS and SQLAlchemy failures the way the CLI reports other errors.
        from dbos._error import DBOSException
        from sqlalchemy.exc import SQLAlchemyError

        try:
            yield
        except (DBOSException, SQLAlchemyError) as exc:
            raise RuntimeError(f"watcher database {self.path}: {exc}") from exc

    @contextmanager
    def _client(self) -> Iterator[DBOSClient]:
        try:
            from dbos import DBOSClient
        except ImportError as exc:
            raise RuntimeError(f"watcher jobs need the dbos package ({exc})") from exc
        _quiet_dbos()
        with self._dbos_errors():
            client = DBOSClient(system_database_url=self.url, application_name=APP_NAME)
            try:
                yield client
            finally:
                client.destroy()

    def _migrate(self) -> None:
        try:
            from dbos import DBOS
        except ImportError as exc:
            raise RuntimeError(f"watchers need the dbos package ({exc})") from exc
        _quiet_dbos()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._dbos_errors():
            DBOS.migrate(self.url)

    def add(self, name: str, trigger: str, command: str) -> None:
        from .conditions import compile_trigger

        if not name.strip() or not command.strip():
            raise ValueError("a watcher needs a name and a shell command")
        compile_trigger(trigger)
        # Fail before registering anything if DBOS cannot run this board's jobs.
        self._migrate()
        with self._database() as db:
            try:
                db.execute("INSERT INTO kbtd_watchers VALUES (?, ?, ?)", (name, trigger, command))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"watcher {name!r} already exists") from exc

    def remove(self, name: str) -> None:
        if self.path.exists():
            with self._database() as db:
                if db.execute("DELETE FROM kbtd_watchers WHERE name = ?", (name,)).rowcount:
                    return
        raise ValueError(f"no watcher named {name!r}")

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM kbtd_watchers ORDER BY name")]

    def emit_many(self, events: Iterable[Event]) -> None:
        if "KBTD_JOB_ID" in os.environ or not self.path.exists():
            return
        from .conditions import compile_trigger

        count, skipped = 0, []
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            watchers = []
            for watcher in db.execute("SELECT * FROM kbtd_watchers ORDER BY name"):
                # One bad definition (for example the earlier TLA+-style syntax)
                # must not stop the other watchers from reacting.
                try:
                    watchers.append((watcher, compile_trigger(watcher["trigger"])))
                except ValueError:
                    skipped.append(watcher["name"])
            for transitions, payload in events:
                kinds = (transitions,) if isinstance(transitions, str) else transitions
                matches = [w for w, matcher in watchers if matcher(kinds, payload)]
                # Keep the original event order, then watcher name, for bare triggers.
                order = {kind: index for index, kind in enumerate(kinds)}
                matches.sort(key=lambda w: order.get(w["trigger"], len(kinds)))
                for watcher in matches:
                    trigger = watcher["trigger"]
                    event = {**payload, "board": str(self.board), "trigger": trigger}
                    if trigger not in kinds:
                        event["events"] = kinds
                    db.execute(
                        "INSERT INTO kbtd_outbox (name, trigger, command, event) VALUES (?, ?, ?, ?)",
                        (watcher["name"], trigger, watcher["command"], json.dumps(event)),
                    )
                count += len(matches)
        if count:
            self.wake()
        if skipped:
            raise SkippedWatchers(skipped)

    def emit(self, trigger: str, payload: dict) -> None:
        self.emit_many([(trigger, payload)])

    # Inspection, retries and cancellations go through the DBOS client.

    def jobs(self, limit: int | None = None) -> list[dict]:
        """Newest first, at most ``limit`` jobs. Wakes the worker so pending work resumes."""
        if not self.path.exists():
            return []
        self.wake()
        found = {row["id"]: row for row in self._outbox_jobs()}
        if self._has_dbos_tables():
            with self._client() as client:
                # Errors load with outputs; the detail column needs them.
                for status in client.list_workflows(name=WORKFLOW, load_input=False,
                                                    sort_desc=True, limit=limit):
                    job = _summary(status)
                    found[job["id"]] = job  # DBOS wins during the outbox handoff
        return [found[key] for key in sorted(found, reverse=True)][:limit]

    def job(self, job_id: int) -> dict:
        """Everything recorded about one job, including the action's output."""
        if self.path.exists():
            with self._database() as db:
                row = db.execute("SELECT * FROM kbtd_outbox WHERE id = ?", (job_id,)).fetchone()
            if row is not None:
                return {**_outbox_summary(row), "command": row["command"], "event": json.loads(row["event"]),
                        "exit_code": None, "output": ""}
            if self._has_dbos_tables():
                with self._client() as client:
                    status = _find(client, job_id)
                    if status is not None:
                        steps = [s for s in client.list_workflow_steps(str(job_id)) if s["function_name"] == STEP]
                        result = steps[-1]["output"] if steps and isinstance(steps[-1]["output"], dict) else {}
                        inputs = _inputs(status)
                        return {**_summary(status), "command": inputs.get("command"),
                                "event": json.loads(inputs["event"]) if "event" in inputs else None,
                                "exit_code": result.get("exit_code"), "output": result.get("output", "")}
        raise ValueError(f"no watcher job numbered {job_id}")

    def retry(self, job_id: int) -> None:
        if self.path.exists():
            with self._database() as db:
                queued = db.execute("SELECT 1 FROM kbtd_outbox WHERE id = ?", (job_id,)).fetchone()
            if queued:
                raise ValueError(_NOT_RETRYABLE)
            if self._has_dbos_tables():
                with self._client() as client:
                    status = _find(client, job_id)
                    if status is not None:
                        if status.status not in _RETRYABLE:
                            raise ValueError(_NOT_RETRYABLE)
                        # Rewinding keeps the job ID and its recorded input, so the
                        # original command and payload run again on the same queue.
                        client.rewind_workflow(str(job_id), start_step=1)
                        self.wake()
                        return
        raise ValueError(f"no watcher job numbered {job_id}")

    def cancel(self, job_id: int) -> None:
        """Take a queued job off the queue before its command starts."""
        if self.path.exists():
            with self._database() as db:
                handing_off = db.execute("SELECT 1 FROM kbtd_outbox WHERE id = ?", (job_id,)).fetchone()
            if handing_off:
                # The worker may be moving this row into DBOS right now. Deleting it
                # here could race that handoff, so wait until DBOS owns the job.
                raise ValueError(f"job {job_id} has not reached the worker yet; try again in a moment")
            if self._has_dbos_tables():
                with self._client() as client:
                    status = _find(client, job_id)
                    if status is not None:
                        if status.status not in _CANCELLABLE:
                            raise ValueError("only queued watcher jobs can be cancelled")
                        # A job the worker dequeues between this check and the cancel
                        # may still run: DBOS stops a workflow only between steps.
                        client.cancel_workflow(str(job_id))
                        return
        raise ValueError(f"no watcher job numbered {job_id}")

    # Worker coordination, standard library only.

    def _has_dbos_tables(self) -> bool:
        with self._database() as db:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workflow_status'"
            ).fetchone() is not None

    def _outbox_jobs(self) -> list[dict]:
        with self._database() as db:
            return [_outbox_summary(row) for row in db.execute("SELECT * FROM kbtd_outbox ORDER BY id")]

    def _outbox(self) -> list[sqlite3.Row]:
        with self._database() as db:
            return db.execute("SELECT * FROM kbtd_outbox ORDER BY id").fetchall()

    def _discard(self, job_id: int) -> None:
        with self._database() as db:
            db.execute("DELETE FROM kbtd_outbox WHERE id = ?", (job_id,))

    def _has_work(self, *, include_running: bool = True) -> bool:
        """Whether a worker would find something to do, read from the documented DBOS tables."""
        statuses = ("ENQUEUED", "PENDING") if include_running else ("ENQUEUED",)
        with self._database() as db:
            if db.execute("SELECT 1 FROM kbtd_outbox LIMIT 1").fetchone():
                return True
            try:
                return db.execute(
                    f"SELECT 1 FROM workflow_status WHERE name = ? AND status IN ({', '.join('?' * len(statuses))}) "
                    "AND (application_version IS NULL OR application_version = ?) LIMIT 1",
                    (WORKFLOW, *statuses, APP_VERSION),
                ).fetchone() is not None
            except sqlite3.OperationalError:
                return False  # DBOS has not created its tables yet

    @contextmanager
    def _worker_lock(self) -> Iterator[sqlite3.Connection | None]:
        # A separate SQLite write lock marks the live worker without holding the
        # database's write lock. The OS releases it if the worker dies, which is
        # what makes it safe for the next worker to let DBOS recover running jobs.
        with closing(sqlite3.connect(self.path.with_suffix(".lock"), timeout=0)) as lock:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                yield None
            else:
                yield lock

    def wake(self) -> None:
        """Start a worker if jobs are waiting and no worker is running."""
        if not self.path.exists() or not self._has_work():
            return
        with self._worker_lock() as lock:
            if lock is None:
                return
        self._start_worker()

    def _start_worker(self) -> None:
        # Import this installation of kbtd (and DBOS) in an isolated interpreter,
        # so modules in the board directory or the caller's directory never load.
        here = Path(__file__).resolve().parent.parent
        cwd = Path.cwd()
        paths = [str(here)] + [p for p in sys.path if p and os.path.isabs(p) and Path(p) != cwd]
        bootstrap = ("import json, sys; sys.path[:0] = json.loads(sys.argv[1]); "
                     "from kbtd.watch_worker import main; main(sys.argv[2])")
        flags = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > _LOG_LIMIT:
                os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
            with self.log_path.open("ab") as log:
                worker = subprocess.Popen(
                    [sys.executable, "-I", "-c", bootstrap, json.dumps(paths), str(self.board)],
                    cwd=self.path.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **flags,
                )
        except OSError as exc:
            raise RuntimeError(f"watcher jobs are queued but the worker could not start: {exc}") from exc
        threading.Thread(target=worker.wait, daemon=True).start()


def _quiet_dbos() -> None:
    # Commands print their own results; DBOS diagnostics belong in the worker log.
    import logging

    logging.getLogger("dbos").setLevel(logging.WARNING)


def _iso(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _outbox_summary(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "trigger": row["trigger"], "status": "queued",
            "attempt": 0, "detail": "", "created_at": row["created_at"], "updated_at": row["created_at"]}


def _detail(status: Any) -> str:
    match status.status:
        case "SUCCESS":
            return "exit code 0"
        case "ERROR":
            # Portable (JSON) errors carry the exception message as an attribute.
            error = status.error
            return getattr(error, "message", None) or ("" if error is None else str(error))
        case "MAX_RECOVERY_ATTEMPTS_EXCEEDED":
            return "worker stopped during every attempt; outcome unknown"
        case "CANCELLED":
            return "cancelled"
    return ""


def _summary(status: Any) -> dict:
    attributes = status.attributes or {}
    return {"id": int(status.workflow_id), "name": attributes.get("watcher", ""),
            "trigger": attributes.get("trigger", ""), "status": _STATUS.get(status.status, status.status.lower()),
            "attempt": int(attributes.get("attempt", 0)), "detail": _detail(status),
            "created_at": _iso(status.created_at), "updated_at": _iso(status.updated_at)}


def _find(client: DBOSClient, job_id: int) -> Any:
    found = client.list_workflows(workflow_ids=[str(job_id)], name=WORKFLOW)
    return found[0] if found else None


def _inputs(status: Any) -> dict:
    inputs = status.input
    if isinstance(inputs, dict):
        return inputs.get("kwargs") or {}
    return getattr(inputs, "kwargs", None) or {}
