import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from kbtd.conditions import compile_trigger
from kbtd.watchers import MAX_RECOVERIES, Watchers

DEADLINE = 20  # seconds; each worker start imports and launches DBOS


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
        # Runs before the directory is removed: let the worker finish idling first.
        self.addCleanup(lambda: self.wait_for(self.worker_stopped, "the worker to exit"))

    def command(self, program):
        (self.board.parent / "action.py").write_text(program, encoding="utf-8")
        args = [sys.executable, "-I", "action.py"]
        return shlex.join(args) if os.name == "posix" else subprocess.list2cmdline(args)

    def wait_for(self, predicate, what):
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline:
            if value := predicate():
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_for_job(self, status, job_id=1):
        last = {}

        def reached():
            nonlocal last
            last = self.watchers.job(job_id)
            return last if last["status"] == status else None

        return self.wait_for(reached, f"job {job_id} to reach {status}; last seen {last}")

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
        # A worker that imported from the board directory would load this.
        (self.board.parent / "sqlite3.py").write_text("raise RuntimeError('wrong sqlite3')")
        command = self.command(
            "import json, os, pathlib, sys, time\n"
            "p = pathlib.Path('unlock')\n"
            "for _ in range(1000):\n"
            "    if p.exists(): break\n"
            "    time.sleep(0.02)\n"
            "else: raise SystemExit(37)\n"
            "keys = ('KBTD_BOARD', 'KBTD_TRIGGER', 'KBTD_JOB_ID', 'KBTD_EVENT', 'KBTD_ATTEMPT')\n"
            "pathlib.Path('observed').write_text(json.dumps([os.environ[k] for k in keys]))\n"
            "print('stdout line'); print('stderr line', file=sys.stderr)\n"
        )
        self.watchers.add("assign", "owner.changed", command)
        title = 'literal $(touch injected) "`"'
        with chdir(self.board.parent):
            self.watchers.emit("owner.changed", {"title": title})
        job = self.wait_for_job("running")
        self.watchers.remove("assign")
        (self.board.parent / "unlock").touch()
        done = self.wait_for_job("succeeded")
        board, trigger, job_id, event, attempt = json.loads((self.board.parent / "observed").read_text())
        self.assertEqual((board, trigger, job_id, attempt), (str(self.board), "owner.changed", str(job["id"]), "1"))
        self.assertEqual(json.loads(event), {"title": title, "board": board, "trigger": trigger})
        self.assertFalse((self.board.parent / "injected").exists())
        self.assertEqual((done["name"], done["command"], done["exit_code"], done["detail"]),
                         ("assign", command, 0, "exit code 0"))
        self.assertEqual(done["event"], json.loads(event))
        self.assertEqual(sorted(done["output"].splitlines()), ["stderr line", "stdout line"])

    def test_failure_and_explicit_retry_after_removal(self):
        command = self.command("import pathlib, sys\nsys.exit(0 if pathlib.Path('ready').exists() else 9)")
        self.watchers.add("done", "task.completed", command)
        self.watchers.emit("task.completed", {"path": "1.1"})
        job = self.wait_for_job("failed")
        self.assertEqual((job["attempt"], job["detail"], job["exit_code"]), (1, "exit code 9", 9))
        self.watchers.remove("done")
        (self.board.parent / "ready").touch()
        self.watchers.retry(job["id"])
        retry = self.wait_for_job("succeeded")
        self.assertEqual((retry["id"], retry["attempt"], retry["command"]), (job["id"], 2, command))
        with self.assertRaisesRegex(ValueError, "only failed or interrupted"):
            self.watchers.retry(job["id"])
        with self.assertRaisesRegex(ValueError, "no watcher job numbered 99"):
            self.watchers.retry(99)

    @unittest.skipUnless(os.name == "posix", "kills the worker's process group")
    def test_stopped_worker_reruns_the_action_until_the_recovery_limit(self):
        starts = self.board.parent / "starts"
        command = self.command(
            "import os, pathlib, time\n"
            "with open('starts', 'a') as f: print(os.environ['KBTD_ATTEMPT'], file=f)\n"
            "if not pathlib.Path('ready').exists():\n"
            "    pathlib.Path('group').write_text(str(os.getpgid(0)))\n"
            "    time.sleep(60)\n"
        )
        self.watchers.add("done", "task.completed", command)
        self.watchers.emit("task.completed", {})
        for attempt in range(1, MAX_RECOVERIES + 2):
            # Stop the worker and its action together, as a reboot would.
            self.wait_for(lambda: starts.exists() and len(starts.read_text().split()) == attempt
                          and (self.board.parent / "group").exists(), f"start {attempt}")
            os.killpg(int((self.board.parent / "group").read_text()), signal.SIGKILL)
            (self.board.parent / "group").unlink()
            self.wait_for(self.worker_stopped, "the worker to stop")
            self.assertEqual(self.watchers.job(1)["status"], "running")
            self.watchers.wake()  # the next worker recovers the interrupted job
        job = self.wait_for_job("interrupted")
        self.assertEqual((job["attempt"], job["detail"]),
                         (MAX_RECOVERIES + 1, "worker stopped during every attempt; outcome unknown"))
        (self.board.parent / "ready").touch()
        self.watchers.retry(1)
        self.assertEqual(self.wait_for_job("succeeded")["attempt"], MAX_RECOVERIES + 2)
        self.assertEqual(starts.read_text().split(), [str(n) for n in range(1, MAX_RECOVERIES + 3)])

    def worker_stopped(self):
        if not self.watchers.path.parent.exists():
            return True
        with self.watchers._worker_lock() as lock:
            return lock is not None

    def test_concurrent_enqueues_each_run_once_in_job_order(self):
        self.watchers.add("reopen", "task.reopened", self.command(
            "import os, pathlib\n"
            "with pathlib.Path('sequence').open('a') as f: print(os.environ['KBTD_JOB_ID'], file=f)\n"
        ))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.watchers.emit("task.reopened", {"i": i}), range(8)))
        self.wait_for_job("succeeded", 8)
        jobs = self.watchers.jobs()
        self.assertEqual([(j["id"], j["status"], j["attempt"]) for j in jobs],
                         [(n, "succeeded", 1) for n in range(8, 0, -1)])
        observed = list(map(int, (self.board.parent / "sequence").read_text().splitlines()))
        self.assertEqual(observed, list(range(1, 9)))

    def test_board_edits_record_jobs_without_importing_dbos(self):
        self.watchers.add("done", "task.completed", "exit 0")
        program = (
            "import sys; from pathlib import Path; from unittest.mock import patch\n"
            "from kbtd.watchers import Watchers\n"
            "w = Watchers(Path(sys.argv[1]))\n"
            "with patch.object(Watchers, '_start_worker') as start:\n"
            "    w.emit('task.completed', {})\n"
            "    w.wake()\n"
            "print(start.call_count, 'dbos' in sys.modules, 'sqlalchemy' in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", program, str(self.board)], capture_output=True,
                                text=True, check=True, cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(result.stdout.split(), ["2", "False", "False"])
        self.assertEqual(self.watchers.job(1)["status"], "queued")
        self.watchers.wake()  # a real worker picks up the job the edit recorded
        self.assertEqual(self.wait_for_job("succeeded")["attempt"], 1)
