"""Persist local watcher definitions and drain their shell actions in a background process."""

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
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import Event


_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchers (
    name TEXT PRIMARY KEY, trigger TEXT NOT NULL, command TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, trigger TEXT NOT NULL,
    command TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, id);
"""
_INTERRUPT_RUNNING = (
    "UPDATE jobs SET status = 'interrupted', detail = 'worker stopped; outcome unknown' "
    "WHERE status = 'running'"
)


class Watchers:
    def __init__(self, board_path: Path):
        self.board = board_path.expanduser().resolve()
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        if not root.is_absolute():
            root = Path.home() / ".local/state"
        key = hashlib.sha256(os.fsencode(self.board)).hexdigest()
        self.path = root / "kbtd" / "watchers" / f"{key}.sqlite3"

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, isolation_level="IMMEDIATE")) as db:
                db.row_factory = sqlite3.Row
                db.executescript(_SCHEMA)
                with db:
                    yield db
        except sqlite3.Error as exc:
            raise RuntimeError(f"watcher database {self.path}: {exc}") from exc

    def add(self, name: str, trigger: str, command: str) -> None:
        # The isolated worker executes this file directly. Only the registering
        # and enqueuing processes need the package's condition parser.
        from .conditions import compile_trigger

        if not name.strip() or not command.strip():
            raise ValueError("a watcher needs a name and a shell command")
        compile_trigger(trigger)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._database() as db:
            try:
                db.execute("INSERT INTO watchers VALUES (?, ?, ?)", (name, trigger, command))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"watcher {name!r} already exists") from exc

    def remove(self, name: str) -> None:
        if self.path.exists():
            with self._database() as db:
                if db.execute("DELETE FROM watchers WHERE name = ?", (name,)).rowcount:
                    return
        raise ValueError(f"no watcher named {name!r}")

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM watchers ORDER BY name")]

    def jobs(self) -> list[dict]:
        if not self.path.exists():
            return []
        self.wake()
        with self._database() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, name, trigger, command, status, attempt, detail, created_at "
                "FROM jobs ORDER BY id DESC"
            )]

    def emit_many(self, events: Iterable[Event]) -> None:
        if "KBTD_JOB_ID" in os.environ or not self.path.exists():
            return
        from .conditions import compile_trigger

        count = 0
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            watchers = db.execute("SELECT * FROM watchers ORDER BY name").fetchall()
            for transitions, payload in events:
                kinds = (transitions,) if isinstance(transitions, str) else transitions
                matches = [w for w in watchers if compile_trigger(w["trigger"])(kinds, payload)]
                # Keep the original event order, then watcher name, for bare triggers.
                order = {kind: index for index, kind in enumerate(kinds)}
                matches.sort(key=lambda w: order.get(w["trigger"], len(kinds)))
                for watcher in matches:
                    trigger = watcher["trigger"]
                    event = {**payload, "board": str(self.board), "trigger": trigger}
                    if trigger not in kinds:
                        event["events"] = kinds
                    db.execute(
                        "INSERT INTO jobs (name, trigger, command, event) VALUES (?, ?, ?, ?)",
                        (watcher["name"], trigger, watcher["command"], json.dumps(event)),
                    )
                count += len(matches)
        if count:
            self.wake()

    def emit(self, trigger: str, payload: dict) -> None:
        self.emit_many([(trigger, payload)])

    def retry(self, job_id: int) -> None:
        if not self.path.exists():
            raise ValueError(f"no watcher job numbered {job_id}")
        self.wake()
        with self._database() as db:
            changed = db.execute(
                "UPDATE jobs SET status = 'queued', detail = '' WHERE id = ? "
                "AND status IN ('failed', 'interrupted')", (job_id,)
            ).rowcount
            if not changed:
                if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                    raise ValueError(f"no watcher job numbered {job_id}")
                raise ValueError("only failed or interrupted watcher jobs can be retried")
        self.wake()

    @contextmanager
    def _worker_lock(self) -> Iterator[sqlite3.Connection | None]:
        # A separate SQLite write lock survives long actions without holding the
        # queue's write lock. The OS releases it if the worker dies.
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
        """Recover abandoned attempts and arrange to run queued jobs, if any."""
        if not self.path.exists():
            return
        with self._database() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE status IN ('running', 'queued') LIMIT 1").fetchone():
                return
            with self._worker_lock() as lock:
                if lock is None:
                    return
                db.execute(_INTERRUPT_RUNNING)
                pending = db.execute("SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1").fetchone()
                # Release before committing. A later enqueue can always wake its own worker.
                lock.rollback()
        if pending:
            self._start_worker()

    def _start_worker(self) -> None:
        # Use this installed file even when launched from a different checkout or
        # from a board directory containing its own Python modules.
        flags = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        try:
            worker = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), str(self.board)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **flags,
            )
        except OSError as exc:
            raise RuntimeError(f"watcher jobs are queued but the worker could not start: {exc}") from exc
        threading.Thread(target=worker.wait, daemon=True).start()

    def _drain(self) -> None:
        with self._database() as db, self._worker_lock() as lock:
            if lock is None:
                return
            with db:
                db.execute(_INTERRUPT_RUNNING)
            while True:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    job = db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
                    if job is None:
                        lock.rollback()
                        return
                    db.execute("UPDATE jobs SET status = 'running', attempt = attempt + 1 WHERE id = ?",
                               (job["id"],))
                status, detail = self._execute(job)
                with db:
                    db.execute("UPDATE jobs SET status = ?, detail = ? WHERE id = ?",
                               (status, detail, job["id"]))

    def _execute(self, job: sqlite3.Row) -> tuple[str, str]:
        env = {**os.environ, "KBTD_BOARD": str(self.board), "KBTD_TRIGGER": job["trigger"],
               "KBTD_JOB_ID": str(job["id"]), "KBTD_EVENT": job["event"]}
        try:
            result = subprocess.run(
                job["command"], shell=True, cwd=self.board.parent, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            return "failed", str(exc)
        status = "succeeded" if result.returncode == 0 else "failed"
        return status, f"exit code {result.returncode}"


if __name__ == "__main__":
    Watchers(Path(sys.argv[1]))._drain()
