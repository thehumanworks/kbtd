import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, closing
from pathlib import Path
from unittest.mock import patch

from kbtd.conditions import compile_trigger
from kbtd.watchers import Watchers


class TriggerTests(unittest.TestCase):
    def test_deeply_grouped_condition_can_be_evaluated_after_registration(self):
        expr = (r"task.completed \/ task.reopened /\ (" * 1000 + "owner.changed" + ")" * 1000)
        self.assertTrue(compile_trigger(expr)(("task.reopened", "owner.changed"), {}))

    def test_boolean_operators_precedence_and_same_edit(self):
        payload = {"column": "In Progress", "after": {"owners": ["codex"], "tags": ["docs"]}}
        expr = r'owner.changed = "codex" \/ tags.changed /\ ~task.status = "In Progress"'
        grouped = r'(owner.changed = "codex" \/ tags.changed) /\ ~task.status = "In Progress"'
        for owner in (False, True):
            for tags in (False, True):
                kinds = tuple(name for name, present in (("owner.changed", owner), ("tags.changed", tags))
                              if present)
                for column in ("Backlog", "In Progress"):
                    with self.subTest(kinds=kinds, column=column):
                        payload["column"] = column
                        self.assertEqual(compile_trigger(expr)(kinds, payload),
                                         owner or (tags and column != "In Progress"))
                        self.assertEqual(compile_trigger(grouped)(kinds, payload),
                                         (owner or tags) and column != "In Progress")
                        self.assertEqual(compile_trigger(r'owner.changed /\ tags.changed')(kinds, payload),
                                         owner and tags)

    def test_value_predicates_use_typed_after_values_and_literal_column_names(self):
        payload = {"column": 'Review "A" / B', "before": {"owners": ["codex"], "tags": ["old"]},
                   "after": {"owners": ["Codex"], "tags": ["docs", "review"], "done": False}}
        for expr, expected in [
            ('owner.changed = "codex"', False), ('owner.changed = "Codex"', True),
            ('tags.changed = "old"', False), ('tags.changed = "docs"', True),
            ('tags.changed = "doc"', False), ('task.reopened = FALSE', True),
            ('task.reopened = TRUE', False), ('task.completed = FALSE', False),
            (r'task.status = "Review \"A\" / B"', True),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(compile_trigger(expr)(("owner.changed", "tags.changed", "task.reopened"),
                                                       payload), expected)


class WatchersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.board = self.root / "work" / "my board.md"
        self.board.parent.mkdir()
        self.board.touch()
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}))
        os.environ.pop("KBTD_JOB_ID", None)
        self.watchers = Watchers(self.board)

    def command(self, program):
        (self.board.parent / "action.py").write_text(program, encoding="utf-8")
        args = [sys.executable, "-I", "action.py"]
        return shlex.join(args) if os.name == "posix" else subprocess.list2cmdline(args)

    def wait_for_job(self, status, job_id=1):
        deadline = time.monotonic() + 8
        with closing(sqlite3.connect(self.watchers.path)) as db:
            db.row_factory = sqlite3.Row
            while time.monotonic() < deadline:
                job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if job and job["status"] == status:
                    return job
                time.sleep(0.02)
        self.fail(f"job {job_id} never reached {status}: {dict(job) if job else 'missing'}")

    def test_board_scoping_and_opt_in(self):
        self.watchers.emit("owner.changed", {})
        self.watchers.wake()
        self.assertEqual(self.watchers.list(), [])
        self.assertEqual(self.watchers.jobs(), [])
        self.assertFalse(self.watchers.path.parent.exists())
        self.watchers.add("assign", "owner.changed", "exit 0")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.watchers.add("assign", "owner.changed", "exit 2")
        with self.assertRaisesRegex(ValueError, "unknown watcher trigger"):
            self.watchers.add("invalid", "unknown", "exit 1")
        other = Watchers(self.board.with_name("other.md"))
        self.assertEqual(other.list(), [])
        self.assertEqual(self.watchers.list(), [
            {"name": "assign", "command": "exit 0", "trigger": "owner.changed"}
        ])
        self.watchers.emit("tags.changed", {})
        with patch.dict(os.environ, {"KBTD_JOB_ID": "17"}):
            self.watchers.emit("owner.changed", {})
        self.assertEqual(self.watchers.jobs(), [])
        self.watchers.remove("assign")
        with self.assertRaisesRegex(ValueError, "no watcher"):
            self.watchers.remove("assign")

    def test_invalid_conditions_are_rejected_before_persisting(self):
        for expr in ("", "task.status", 'task.status = TRUE', "owner.changed = codex", "()",
                     'owner.changed = "broken', r'owner.changed = "bad\q"', "(owner.changed",
                     "owner.changed /\\", "tags.changed)", "tags.changed unknown", "unknown",
                     "task.completed = 1", "owner.changed == 'codex'", "owner.changed | tags.changed"):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                self.watchers.add("invalid", expr, "exit 1")
            self.assertFalse(self.watchers.path.exists())

    def test_detached_action_receives_literal_event_and_uses_board_directory(self):
        # A worker started with python -m in the board directory could import this.
        (self.board.parent / "sqlite3.py").write_text("raise RuntimeError('wrong sqlite3')")
        command = self.command(
            "import json, os, pathlib, time\n"
            "p = pathlib.Path('unlock')\n"
            "for _ in range(400):\n"
            "    if p.exists(): break\n"
            "    time.sleep(0.02)\n"
            "else: raise SystemExit(37)\n"
            "data = [os.environ[k] for k in ('KBTD_BOARD', 'KBTD_TRIGGER', 'KBTD_JOB_ID', 'KBTD_EVENT')]\n"
            "pathlib.Path('observed').write_text(json.dumps(data))\n"
        )
        self.watchers.add("assign", "owner.changed", command)
        title = 'literal $(touch injected) "`"'
        with chdir(self.board.parent):
            self.watchers.emit("owner.changed", {"title": title})
        job = self.wait_for_job("running")
        self.watchers.remove("assign")
        (self.board.parent / "unlock").touch()
        self.wait_for_job("succeeded")
        board, trigger, job_id, event = json.loads((self.board.parent / "observed").read_text())
        self.assertEqual((board, trigger, job_id), (str(self.board), "owner.changed", str(job["id"])))
        self.assertEqual(json.loads(event), {"title": title, "board": board, "trigger": trigger})
        self.assertFalse((self.board.parent / "injected").exists())

    def test_failure_and_explicit_retry_after_removal(self):
        command = self.command("import pathlib, sys\nsys.exit(0 if pathlib.Path('ready').exists() else 9)")
        self.watchers.add("done", "task.completed", command)
        self.watchers.emit("task.completed", {"path": "1.1"})
        job = self.wait_for_job("failed")
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(job["detail"], "exit code 9")
        self.watchers.remove("done")
        (self.board.parent / "ready").touch()
        self.watchers.retry(job["id"])
        retry = self.wait_for_job("succeeded")
        self.assertEqual((retry["id"], retry["attempt"]), (job["id"], 2))
        with self.assertRaisesRegex(ValueError, "only failed or interrupted"):
            self.watchers.retry(job["id"])

    def test_crash_recovery_only_runs_queued_attempts(self):
        self.watchers.add("done", "task.completed", "exit 0")
        with patch.object(self.watchers, "_start_worker") as start:
            self.watchers.emit_many([("task.completed", {}), ("task.completed", {})])
            start.assert_called_once()
        # Simulate a process dying after claiming job 1, leaving job 2 unclaimed.
        with closing(sqlite3.connect(self.watchers.path)) as db, db:
            db.execute("UPDATE jobs SET status='running', attempt=1 WHERE id=1")
        self.watchers.wake()
        self.wait_for_job("succeeded", 2)
        newest, oldest = self.watchers.jobs()
        self.assertEqual((newest["id"], newest["attempt"]), (2, 1))
        self.assertEqual((oldest["status"], oldest["attempt"]), ("interrupted", 1))
        self.watchers.retry(oldest["id"])
        self.assertEqual(self.wait_for_job("succeeded", 1)["attempt"], 2)

    def test_concurrent_enqueues_each_run_once_in_job_order(self):
        self.watchers.add("reopen", "task.reopened", self.command(
            "import os, pathlib\n"
            "with pathlib.Path('sequence').open('a') as f: print(os.environ['KBTD_JOB_ID'], file=f)\n"
        ))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.watchers.emit("task.reopened", {"i": i}), range(8)))
        self.wait_for_job("succeeded", 8)
        jobs = self.watchers.jobs()
        self.assertEqual([(j["status"], j["attempt"]) for j in jobs], [("succeeded", 1)] * 8)
        observed = list(map(int, (self.board.parent / "sequence").read_text().splitlines()))
        self.assertEqual(observed, list(range(1, 9)))
