import json
import os
import random
import re
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
from kbtd.watchers import MAX_RECOVERIES, SkippedWatchers, Watchers

DEADLINE = 20  # seconds; each worker start imports and launches DBOS


EVENTS = ("owner.changed", "tags.changed", "task.completed", "task.reopened")
NAMES = ["codex", "Codex", "a b", 'q"x', "back\\slash", "ü", ""]
COLUMNS = ["Backlog", "In Progress", 'Review "A" / B']


def random_condition(rng, depth):
    """A random condition as (Python source, reference evaluator)."""
    if depth == 0 or rng.random() < 0.3:
        match rng.randrange(6):
            case 0:
                event = rng.choice(EVENTS)
                return event, lambda kinds, after, column: event in kinds
            case 1:
                field, name = rng.choice(["owners", "tags"]), rng.choice(NAMES)
                op = rng.choice(["in", "not in"])
                return (f"{json.dumps(name)} {op} task.{field}",
                        lambda kinds, after, column: (name in after[field]) == (op == "in"))
            case 2:
                column, op = rng.choice(COLUMNS), rng.choice(["==", "!="])
                return (f"task.status {op} {json.dumps(column)}",
                        lambda kinds, after, column_now: (column_now == column) == (op == "=="))
            case 3:
                options = rng.sample(COLUMNS, 2)
                return (f"task.status in {json.dumps(options)}",
                        lambda kinds, after, column: column in options)
            case 4:
                return "task.done", lambda kinds, after, column: after["done"]
            case _:
                field = rng.choice(["owners", "tags"])
                return f"task.{field}", lambda kinds, after, column: bool(after[field])
    op = rng.choice(["and", "or", "not"])
    if op == "not":
        text, inner = random_condition(rng, depth - 1)
        return f"not ({text})", lambda *args: not inner(*args)
    (left_text, left), (right_text, right) = random_condition(rng, depth - 1), random_condition(rng, depth - 1)
    space = rng.choice([" ", "  ", "\t"])
    text = f"({left_text}){space}{op}{space}({right_text})"
    if op == "and":
        return text, lambda *args: left(*args) and right(*args)
    return text, lambda *args: left(*args) or right(*args)


class TriggerTests(unittest.TestCase):
    def test_precedence_follows_python_and_reads_one_edit(self):
        payload = {"column": "In Progress", "after": {"owners": ["codex"], "tags": ["docs"]}}
        expr = 'owner.changed and "codex" in task.owners or tags.changed and not task.status == "In Progress"'
        grouped = '(owner.changed or tags.changed) and task.status != "In Progress"'
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
                        self.assertEqual(compile_trigger("owner.changed and tags.changed")(kinds, payload),
                                         owner and tags)

    def test_fields_read_the_task_after_the_edit(self):
        payload = {"section": "Tasks", "column": 'Review "A" / B', "before": {"owners": ["codex"]},
                   "after": {"text": "Fix the bug @Codex", "owners": ["Codex"], "tags": ["docs", "review"],
                             "done": False}}
        kinds = ("owner.changed", "task.reopened")
        for expr, expected in [
            ('"codex" in task.owners', False), ('"Codex" in task.owners', True),
            ('"doc" in task.tags', False), ('"docs" in task.tags and "review" in task.tags', True),
            ("task.reopened and not task.done", True), ("task.completed", False),
            ("task.status == 'Review \"A\" / B'", True), ('task.status in ["Backlog", "Ready"]', False),
            ('task.section == "Tasks"', True), ('"bug" in task.text', True),
            ("task.owners", True), ("not task.tags", False), ('task.tags == ["docs", "review"]', True),
            ("task.owners == []", False),
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(compile_trigger(expr)(kinds, payload), expected)
        self.assertFalse(compile_trigger('"codex" in task.owners or task.done')((), {}))

    def test_matches_a_reference_evaluator_on_random_conditions(self):
        rng = random.Random(20261126)
        for _ in range(3000):
            text, reference = random_condition(rng, rng.randrange(1, 6))
            kinds = tuple(event for event in EVENTS if rng.random() < 0.5)
            after = {"owners": rng.sample(NAMES, rng.randrange(3)), "tags": rng.sample(NAMES, rng.randrange(3)),
                     "done": rng.random() < 0.5}
            column = rng.choice(COLUMNS)
            with self.subTest(text=text, kinds=kinds, after=after, column=column):
                self.assertEqual(compile_trigger(text)(kinds, {"column": column, "after": after}),
                                 reference(kinds, after, column))

    def test_mistakes_are_rejected_with_a_reason(self):
        for expr, reason in [
            ("", "needs an event name"),
            ('owner.changed = "codex"', "compare with =="),
            (r"owner.changed /\ tags.changed", "use and, or, not"),
            ("~owner.changed", "use 'not'"),
            ('owner.changed and "codex"', "fixed value is not a condition"),
            ('task.owners == "codex"', 'write "name" in task.owners'),
            ('"a" == "a"', "two fixed values"),
            ("True", "fixed value"),
            ("task.status", "is a string, not a condition"),
            ("task.owner", "did you mean 'task.owners'"),
            ("TRUE", "write True or False"),
            ("1", "only quoted strings"),
            ('"a" < task.status', "only ==, !=, in and not in"),
            ('task.status == "a" == "a"', "instead of chaining"),
            ("task.owners in task.tags", "'in' needs a string on the left"),
            ("task.status in [task.text]", "only contain quoted strings"),
            ('__import__("os").system("id")', "Call expressions are not allowed"),
            ("().__class__", "only event names and task fields"),
            ("[x for x in task.tags]", "ListComp expressions"),
            ("owner.changed\nand tags.changed", "wrap a trigger that spans lines in parentheses"),
            ("not " * 100 + "owner.changed", "nested too deeply"),
            ("(" * 300 + "owner.changed" + ")" * 300, "too many nested parentheses"),
            ("owner.changed or " * 400 + "tags.changed", "at most 4096 characters"),
        ]:
            with self.subTest(expr=expr[:40]), self.assertRaisesRegex(ValueError, re.escape(reason)):
                compile_trigger(expr)
        self.assertTrue(compile_trigger("(owner.changed\n and tags.changed)")(("owner.changed", "tags.changed"), {}))


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
        with self.assertRaisesRegex(ValueError, "unknown name 'unknown'"):
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
        for expr in ("", "task.status", "owner.changed = 'codex'", r"owner.changed /\ tags.changed", "()",
                     'owner.changed and "broken', "(owner.changed", "owner.changed and", "tags.changed)",
                     "tags.changed unknown", "unknown", "task.completed == 1", "owner.changed | tags.changed"):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                self.watchers.add("invalid", expr, "exit 1")
            self.assertFalse(self.watchers.path.exists())

    def test_an_invalid_stored_trigger_does_not_stop_other_watchers(self):
        self.watchers.add("valid", "task.completed", "exit 0")
        with self.watchers._database() as db:  # a definition from the earlier TLA+-style syntax
            db.execute("INSERT INTO kbtd_watchers VALUES (?, ?, ?)",
                       ("legacy", r'task.completed /\ task.status = "Done"', "exit 0"))
        with patch.object(Watchers, "_start_worker"), \
                self.assertRaisesRegex(SkippedWatchers, "Skipped watcher 'legacy'"):
            self.watchers.emit("task.completed", {"column": "Done"})
        self.assertEqual([(job["name"], job["status"]) for job in self.watchers.jobs()], [("valid", "queued")])

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
