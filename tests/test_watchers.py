import json
import os
import random
import re
import shlex
import signal
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
from kbtd.watchers import MAX_RECOVERIES, SkippedWatchers, Watchers

DEADLINE = 20  # seconds; each worker start imports and launches DBOS


EVENTS = ("owner.changed", "tags.changed", "task.completed", "task.reopened", "task.status.changed",
          "task.description.changed")
NAMES = ["codex", "Codex", "a b", 'q"x', "back\\slash", "ü", ""]
DESCRIPTIONS = ["", "Fix the login", "fix\nlogin", 'Quote "x" here', "ü"]
WORDS = ["login", "fix", "\n", '"x"', "ü", ""]
COLUMNS = ["Backlog", "In Progress", 'Review "A" / B']

# Stand-in for an agent service that accepts a durable idempotency key.
RECORD_LAUNCH = """\
import os, sqlite3
from contextlib import closing
with closing(sqlite3.connect("launcher.db")) as db, db:
    db.execute("CREATE TABLE IF NOT EXISTS runs (board TEXT, job TEXT, event TEXT, PRIMARY KEY (board, job))")
    db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
               (os.environ["KBTD_BOARD"], os.environ["KBTD_JOB_ID"], os.environ["KBTD_EVENT"]))
"""


def random_condition(rng, depth):
    """A random condition as (Python source, reference evaluator of (event names, payload))."""
    if depth == 0 or rng.random() < 0.3:
        match rng.randrange(12):
            case 0:
                event = rng.choice(EVENTS)
                return event, lambda kinds, payload: event in kinds
            case 1:
                field, name = rng.choice(["owners", "tags"]), rng.choice(NAMES)
                op = rng.choice(["in", "not in"])
                return (f"{json.dumps(name)} {op} task.{field}",
                        lambda kinds, payload: (name in payload["after"][field]) == (op == "in"))
            case 2:
                column, op = rng.choice(COLUMNS), rng.choice(["==", "!="])
                return (f"task.status {op} {json.dumps(column)}",
                        lambda kinds, payload: (payload["column"] == column) == (op == "=="))
            case 3:
                options = rng.sample(COLUMNS, 2)
                return (f"task.status in {json.dumps(options)}",
                        lambda kinds, payload: payload["column"] in options)
            case 4:
                return "task.done", lambda kinds, payload: payload["after"]["done"]
            case 5:
                field = rng.choice(["owners", "tags"])
                return f"task.{field}", lambda kinds, payload: bool(payload["after"][field])
            case 9:
                word = rng.choice(WORDS)
                return (f"{json.dumps(word)} in task.description",
                        lambda kinds, payload: word in payload["after"]["description"])
            case 10:
                side, word = rng.choice(["from", "to"]), rng.choice(WORDS)
                snapshot = "before" if side == "from" else "after"
                return (f"{json.dumps(word)} in task.description.changed_{side}",
                        lambda kinds, payload: "task.description.changed" in kinds
                        and word in payload[snapshot]["description"])
            case 11:
                side, text = rng.choice(["from", "to"]), rng.choice(DESCRIPTIONS)
                snapshot = "before" if side == "from" else "after"
                return (f"task.description.changed_{side} == {json.dumps(text)}",
                        lambda kinds, payload: "task.description.changed" in kinds
                        and payload[snapshot]["description"] == text)
            case 6:
                column = rng.choice(COLUMNS)
                return (f"task.status.changed_to == {json.dumps(column)}",
                        lambda kinds, payload: "task.status.changed" in kinds and payload["after"]["status"] == column)
            case 7:
                options = rng.sample(COLUMNS, 2)
                return (f"task.status.changed_from in {json.dumps(options)}",
                        lambda kinds, payload: "task.status.changed" in kinds and payload["before"]["status"] in options)
            case _:
                name, field = rng.choice(NAMES), rng.choice(["added", "removed"])

                def transitioned(kinds, payload):
                    was_owner = name in payload["before"]["owners"]
                    is_owner = name in payload["after"]["owners"]
                    if "owner.changed" not in kinds:
                        return False
                    if field == "added":
                        return is_owner and not was_owner
                    return was_owner and not is_owner

                return f"{json.dumps(name)} in owner.{field}", transitioned
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

    def test_status_change_names_match_only_moves(self):
        moved = (("task.status.changed",), {"column": "Done", "before": {"status": "Backlog"},
                                            "after": {"status": "Done"}})
        edited = (("owner.changed",), {"column": "Done", "before": {"status": "Done"}, "after": {"status": "Done"}})
        for expr, on_move, on_edit in [
            ("task.status.changed", True, False),
            ('task.status.changed_to == "Done"', True, False),
            ('"Done" == task.status.changed_to', True, False),
            ('task.status.changed_from == "Backlog"', True, False),
            ('task.status.changed_from in ["Backlog", "Ready"]', True, False),
            ('task.status.changed and not task.status.changed_to == "Done"', False, False),
            ('not task.status.changed_from == "Backlog"', False, True),
            ('task.status == "Done"', True, True),  # the column now, whatever changed
            ('"Do" in task.status.changed_to', True, False),  # substring, as for task.status
        ]:
            with self.subTest(expr=expr):
                self.assertEqual(compile_trigger(expr)(*moved), on_move)
                self.assertEqual(compile_trigger(expr)(*edited), on_edit)

    def test_description_names_match_whole_text_part_of_it_and_its_change(self):
        described = (("task.description.changed",),
                     {"before": {"description": "Users see a 500"},
                      "after": {"description": "Users see a 500\nafter a password reset"}})
        # Another edit of the same card: its description is the new one, unchanged.
        owner = (("owner.changed",), {"before": {"description": described[1]["after"]["description"]},
                                      "after": {"description": described[1]["after"]["description"]}})
        cleared = (("task.description.changed",), {"before": {"description": "old"}, "after": {"description": ""}})
        for expr, on_change, on_owner, on_clear in [
            ("task.description.changed", True, False, True),
            ('task.description == "Users see a 500\\nafter a password reset"', True, True, False),
            ('"password" in task.description', True, True, False),
            ('task.description == ""', False, False, True),
            ('task.description.changed_from == "Users see a 500"', True, False, False),
            ('"reset" in task.description.changed_to', True, False, False),
            ('"reset" in task.description.changed_from', False, False, False),
            ('task.description.changed_to == ""', False, False, True),  # removed
            ('task.description.changed_from == ""', False, False, False),  # added
            ('task.description.changed_from in ["old", "older"]', False, False, True),
            ('task.description.changed and not "reset" in task.description.changed_to', False, False, True),
        ]:
            with self.subTest(expr=expr):
                matcher = compile_trigger(expr)
                self.assertEqual((matcher(*described), matcher(*owner), matcher(*cleared)),
                                 (on_change, on_owner, on_clear))
        # A malformed or older payload has no description to compare.
        for payload in ({}, {"before": None, "after": {"description": 3}}):
            self.assertFalse(compile_trigger('"x" in task.description.changed_to')(described[0], payload))

    def test_matches_a_reference_evaluator_on_random_conditions(self):
        rng = random.Random(20261126)
        for _ in range(3000):
            text, reference = random_condition(rng, rng.randrange(1, 6))
            kinds = tuple(event for event in EVENTS if rng.random() < 0.5)
            after = {"owners": rng.sample(NAMES, rng.randrange(3)), "tags": rng.sample(NAMES, rng.randrange(3)),
                     "done": rng.random() < 0.5}
            column, previous = rng.choice(COLUMNS), rng.choice(COLUMNS)
            payload = {"column": column,
                       "before": {"owners": rng.sample(NAMES, rng.randrange(3)), "status": previous,
                                  "description": rng.choice(DESCRIPTIONS)},
                       "after": {**after, "status": column, "description": rng.choice(DESCRIPTIONS)}}
            with self.subTest(text=text, kinds=kinds, payload=payload):
                self.assertEqual(compile_trigger(text)(kinds, payload), reference(kinds, payload))

    def test_owner_differences_use_both_snapshots(self):
        payload = {"before": {"owners": ["sam", "Codex", "sam", "alex"]},
                   "after": {"owners": ["codex", "bea", "alex", "bea"]}}
        kinds = ("owner.changed", "tags.changed")
        for expr in ['owner.added == ["bea", "codex"]', 'owner.removed == ["Codex", "sam"]',
                     'owner.added and owner.removed', '"sam" in owner.removed and "codex" in owner.added',
                     '"alex" not in owner.added and "alex" not in owner.removed']:
            with self.subTest(expr=expr):
                self.assertTrue(compile_trigger(expr)(kinds, payload))
        no_delta = compile_trigger("owner.added == [] and owner.removed == []")
        self.assertTrue(no_delta(("tags.changed",), payload))
        for invalid in [None, {}, {"owners": "codex"}, {"owners": [42]}]:
            for side in ("before", "after"):
                with self.subTest(side=side, invalid=invalid):
                    self.assertTrue(no_delta(kinds, {**payload, side: invalid}))
        for missing in ("before", "after"):
            self.assertTrue(no_delta(kinds, {side: data for side, data in payload.items() if side != missing}))
        self.assertTrue(compile_trigger("owner.changed")(kinds, {}))

    def test_mistakes_are_rejected_with_a_reason(self):
        for expr, reason in [
            ("", "needs an event name"),
            ('owner.changed = "codex"', "compare with =="),
            (r"owner.changed /\ tags.changed", "use and, or, not"),
            ("~owner.changed", "use 'not'"),
            ('owner.changed and "codex"', "fixed value is not a condition"),
            ('task.owners == "codex"', 'write "codex" in task.owners'),
            ('owner.added == "codex"', 'write "codex" in owner.added'),
            ('task.status == ["Backlog"]', 'write task.status in ["Backlog"]'),
            ('owner.assigned("codex")', "Call expressions are not allowed"),
            ("owner.removed[0]", "Subscript expressions are not allowed"),
            ("owner.added.__class__", "unknown name"),
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
            ('task.status.changed_to = "Done"', "compare with =="),
            ('task.status.changed_to != "Done"', "would also match every other edit"),
            ('task.status.changed_from not in ["Backlog"]', "write task.status.changed and not"),
            ("task.status.changed_to", "use task.status.changed to test for the change"),
            ("task.status.changed_to in task.text", 'compare task.status.changed_to with == "text"'),
            ('task.description.changed_to != ""', "write task.description.changed and not"),
            ('"x" not in task.description.changed_from', "would also match every other edit"),
            ("task.description.changed_from", "use task.description.changed to test for the change"),
            ("task.description.changed_to == task.description.changed_from", "compare task.description"),
            ('task.description.changed_to in "abc"', 'compare task.description.changed_to with'),
            ('task.description == ["x"]', 'write task.description in ["x"]'),
            ("task.descripton", "did you mean 'task.description'"),
            ("task.status.changes_to", "did you mean 'task.status.changed_to'"),
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

    def set_owner(self, value):
        subprocess.run([sys.executable, "-m", "kbtd", "-f", str(self.board), "owner", "1", "1", "1", value],
                       check=True, stdout=subprocess.DEVNULL)

    def queue_assignment(self, command):
        self.board.write_text("# Tasks\n## Backlog\n- [ ] Work\n")
        self.watchers.add("assign", '"codex" in owner.added and task.status == "Backlog"', command)
        self.set_owner("codex")

    def assert_one_launch(self, job):
        with closing(sqlite3.connect(self.board.parent / "launcher.db")) as db:
            runs = list(db.execute("SELECT board, job, event FROM runs"))
        self.assertEqual([(board, job_id, json.loads(event)) for board, job_id, event in runs],
                         [(str(self.board), str(job["id"]), job["event"])])
        self.assertEqual([entry["id"] for entry in self.watchers.jobs()], [job["id"]])

    def test_failure_and_explicit_retry_after_removal(self):
        command = self.command("import pathlib\nif not pathlib.Path('ready').exists(): raise SystemExit(9)\n"
                               + RECORD_LAUNCH)
        self.queue_assignment(command)
        job = self.wait_for_job("failed")
        self.assertEqual((job["attempt"], job["detail"], job["exit_code"]), (1, "exit code 9", 9))
        self.assertFalse((self.board.parent / "launcher.db").exists())
        self.watchers.remove("assign")
        (self.board.parent / "ready").touch()
        self.watchers.retry(job["id"])
        retry = self.wait_for_job("succeeded")
        self.assertEqual((retry["id"], retry["attempt"], retry["command"]), (job["id"], 2, command))
        self.assertEqual(retry["event"], job["event"])
        self.assert_one_launch(job)
        with self.assertRaisesRegex(ValueError, "only failed, interrupted or cancelled"):
            self.watchers.retry(job["id"])
        with self.assertRaisesRegex(ValueError, "no watcher job numbered 99"):
            self.watchers.retry(99)

    @unittest.skipUnless(os.name == "posix", "kills the worker's process group")
    def test_stopped_worker_reruns_the_action_until_the_recovery_limit(self):
        starts = self.board.parent / "starts"
        command = self.command(
            RECORD_LAUNCH
            + "import pathlib, time\n"
            "with open('starts', 'a') as f: print(os.environ['KBTD_ATTEMPT'], file=f)\n"
            "if not pathlib.Path('ready').exists():\n"
            "    pathlib.Path('group').write_text(str(os.getpgid(0)))\n"
            "    time.sleep(60)\n"
        )
        self.queue_assignment(command)
        for attempt in range(1, MAX_RECOVERIES + 2):
            # Stop the worker and its action together, as a reboot would.
            self.wait_for(lambda: starts.exists() and len(starts.read_text().split()) == attempt
                          and (self.board.parent / "group").exists(), f"start {attempt}")
            if attempt == 1:
                self.set_owner("--clear")  # Recovery must use the original assignment.
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
        self.assertEqual(job["event"]["after"]["owners"], ["codex"])
        self.assert_one_launch(job)

    def test_cancel_takes_only_queued_jobs_off_the_queue(self):
        runs = self.board.parent / "runs"
        self.watchers.add("slow", "task.completed", self.command(
            "import os, pathlib, time\n"
            "with open('runs', 'a') as f: print(os.environ['KBTD_JOB_ID'], file=f)\n"
            "for _ in range(1000):\n"
            "    if pathlib.Path('unlock').exists(): break\n"
            "    time.sleep(0.02)\n"
        ))
        with patch.object(Watchers, "_start_worker"):
            self.watchers.emit("task.completed", {})
            self.watchers.emit("task.completed", {})
            with self.assertRaisesRegex(ValueError, "job 2 has not reached the worker"):
                self.watchers.cancel(2)
        self.watchers.wake()
        self.wait_for_job("running", 1)
        self.wait_for(lambda: not self.watchers._outbox(), "job 2 to reach DBOS")
        with self.assertRaisesRegex(ValueError, "only queued"):
            self.watchers.cancel(1)
        self.watchers.cancel(2)
        self.assertEqual(self.watchers.job(2)["status"], "cancelled")
        (self.board.parent / "unlock").touch()
        self.wait_for_job("succeeded", 1)
        self.wait_for(self.worker_stopped, "the worker to go idle")
        self.assertEqual(runs.read_text().split(), ["1"])
        self.watchers.retry(2)
        self.assertEqual(self.wait_for_job("succeeded", 2)["attempt"], 1)
        self.assertEqual(runs.read_text().split(), ["1", "2"])
        self.assertEqual([job["id"] for job in self.watchers.jobs(limit=1)], [2])
        with self.assertRaisesRegex(ValueError, "no watcher job numbered 99"):
            self.watchers.cancel(99)

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
