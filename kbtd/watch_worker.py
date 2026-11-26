"""Detached worker that runs one board's watcher jobs as DBOS durable workflows.

``Watchers._start_worker`` launches ``main`` in an isolated interpreter. Only
one worker per board holds the lock, so every PENDING workflow it finds at
launch belongs to a worker that stopped, and DBOS can safely recover it.
"""

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from dbos import (
    DBOS,
    SetEnqueueOptions,
    SetWorkflowAttributes,
    SetWorkflowID,
    WorkflowSerializationFormat,
)

from .watchers import APP_NAME, APP_VERSION, MAX_RECOVERIES, QUEUE, STEP, WORKFLOW, Watchers

POLL_SECONDS = 0.1
# Startup recovery runs in a DBOS background thread; give it time to re-enqueue
# the PENDING jobs of a worker that stopped before treating them as stuck.
RECOVERY_GRACE_SECONDS = 5.0
# Shutdown only waits when a job started between the idle check and DBOS.destroy.
DRAIN_SECONDS = 24 * 60 * 60
OUTPUT_LIMIT = 8192

_active = 0
_active_lock = threading.Lock()


class ActionFailed(Exception):
    """A watcher command exited non-zero or could not start (an expected outcome)."""


def _tail(stream: Any) -> str:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - OUTPUT_LIMIT))
    return stream.read().decode("utf-8", "replace")


@DBOS.step(name=STEP)
def run_command(*, board: str, trigger: str, command: str, event: str) -> dict:
    job = DBOS.workflow_id
    assert job is not None
    # Count real starts here: the step body runs again only if it never finished.
    attributes = DBOS.get_workflow_status(job).attributes or {}
    attempt = int(attributes.get("attempt", 0)) + 1
    DBOS.update_workflow_attributes(job, {"attempt": attempt}, merge=True)
    env = {**os.environ, "KBTD_BOARD": board, "KBTD_TRIGGER": trigger, "KBTD_JOB_ID": job,
           "KBTD_EVENT": event, "KBTD_ATTEMPT": str(attempt)}
    # A file rather than a pipe: a background process that inherits the
    # command's output must not keep the job running.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                command, shell=True, cwd=Path(board).parent, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            )
        except (OSError, ValueError) as exc:
            return {"exit_code": None, "error": str(exc), "output": ""}
        return {"exit_code": result.returncode, "output": _tail(output)}


@DBOS.workflow(name=WORKFLOW, max_recovery_attempts=MAX_RECOVERIES,
               serialization_type=WorkflowSerializationFormat.PORTABLE)
def watcher_action(*, board: str, name: str, trigger: str, command: str, event: str) -> dict:
    global _active
    with _active_lock:
        _active += 1
    try:
        result = run_command(board=board, trigger=trigger, command=command, event=event)
    finally:
        with _active_lock:
            _active -= 1
    if result["exit_code"] is None:
        raise ActionFailed(result["error"])
    if result["exit_code"]:
        raise ActionFailed(f"exit code {result['exit_code']}")
    return result


def _hand_off(watchers: Watchers) -> int:
    """Move outbox jobs into DBOS in job order; the job number is the workflow ID."""
    rows = watchers._outbox()
    for row in rows:
        # Priority is the job number, so jobs run in creation order even when
        # several were enqueued in the same millisecond. Reusing the workflow
        # ID makes a repeated handoff after a crash return the existing job.
        attributes = {"watcher": row["name"], "trigger": row["trigger"]}
        with SetWorkflowID(str(row["id"])), SetEnqueueOptions(priority=row["id"]), \
                SetWorkflowAttributes(attributes):
            DBOS.enqueue_workflow(QUEUE, watcher_action, board=str(watchers.board), name=row["name"],
                                  trigger=row["trigger"], command=row["command"], event=row["event"])
        watchers._discard(row["id"])
    return len(rows)


def _busy(launched: float) -> bool:
    if _active:
        return True
    waiting = [w for w in DBOS.list_workflows(status=["ENQUEUED", "PENDING"], name=WORKFLOW,
                                              load_input=False, load_output=False)
               if w.app_version in (None, "", APP_VERSION)]
    if any(w.status == "ENQUEUED" for w in waiting):
        return True
    # PENDING jobs that are not running here await startup recovery.
    return bool(waiting) and time.monotonic() - launched < RECOVERY_GRACE_SECONDS


def _serve(watchers: Watchers) -> None:
    logging.getLogger("dbos").addFilter(
        lambda record: not (record.exc_info and isinstance(record.exc_info[1], ActionFailed)))
    DBOS(config={
        "name": APP_NAME, "application_version": APP_VERSION,
        "system_database_url": watchers.url, "log_level": "WARNING",
    })
    DBOS.launch()
    launched = time.monotonic()
    try:
        DBOS.register_queue(QUEUE, worker_concurrency=1, polling_interval_sec=POLL_SECONDS)
        idle = 0
        # Two idle checks in a row cover a job that was dequeued but not yet entered.
        while idle < 2:
            moved = _hand_off(watchers)
            idle = 0 if moved or _busy(launched) else idle + 1
            time.sleep(POLL_SECONDS)
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=DRAIN_SECONDS)


def main(board: str) -> None:
    watchers = Watchers(Path(board))
    with watchers._worker_lock() as lock:
        if lock is None:
            return  # another worker is running and will see new jobs
        _serve(watchers)
    # A job committed after the last idle check found the lock held; start again.
    if watchers._has_work(include_running=False):
        watchers.wake()
