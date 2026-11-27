import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kbtd.cli import main
from kbtd.storage import BoardFile
from kbtd.watchers import Watchers


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "my board.md"

    def cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        if ok:
            self.assertEqual(code, 0, err.getvalue())
            return out.getvalue()
        self.assertNotEqual(code, 0)
        return err.getvalue()

    def test_end_to_end(self):
        path = str(self.file)
        self.cli("init", path)
        self.cli("add", "-f", path, "Tasks", "Backlog", "Design a prototype")
        self.cli("-f", path, "move", "1", "1", "1", "In Progress")
        self.cli("--file", path, "toggle", "Tasks", "2", "1")
        self.cli("-f", path, "edit", "1", "2", "1", "Design mockups")
        listing = self.cli("list", "Tasks", "In Progress", "-f", path)
        self.assertIn("## 2. In Progress", listing)
        self.assertIn("1. [x] Design mockups", listing)
        self.assertNotIn("Backlog", listing)
        self.assertIn("- [x] Design mockups", self.file.read_text())
        self.cli("-f", path, "delete", "1", "2", "1")
        self.assertNotIn("Design mockups", self.file.read_text())

    def test_cli_and_tui_share_default_file(self):
        with chdir(self.file.parent):
            self.cli("init")
            self.assertTrue(Path("TODO.md").is_file())
            self.assertIn("Backlog", self.cli("list"))
            self.assertIn("interactive terminal", self.cli(ok=False))
            self.assertIn("interactive terminal", self.cli("TODO.md", ok=False))

    def test_priority_moves_swap_root_and_child_subtrees_in_place(self):
        path = str(self.file)
        header = b"# Board\n## Queue\r\n"
        parent = b"- [ ] Parent\r\n"
        first = b"  - [ ] Same @ada\r\n    child note\r\n    - [x] Grandchild\r\n"
        between = b"\r\n  parent note\r\n  - [-] declined\r\n"
        second = b"  - [x] Same #docs\r\n"
        gap = b"\r\ncolumn prose\r\n"
        other = b"- [x] Other"
        original = header + parent + first + between + second + gap + other
        self.file.write_bytes(original)

        self.cli("-f", path, "up", "Board", "Queue", "1.2")
        swapped = parent + second + between + first
        self.assertEqual(self.file.read_bytes(), header + swapped + gap + other)
        self.assertIn("1.2.1. [x] Grandchild", self.cli("-f", path, "list"))

        self.cli("-f", path, "down", "1", "1", "1")
        self.assertEqual(self.file.read_bytes(), header + other + b"\r\n" + gap + swapped.rstrip(b"\r\n"))
        with patch.object(BoardFile, "save", side_effect=AssertionError("boundary move wrote to disk")):
            for direction, task in [("up", "1"), ("down", "2"), ("down", "2.2"), ("up", "2.2.1")]:
                self.cli("-f", path, direction, "1", "1", task)
        self.cli("-f", path, "up", "1", "1", "2")
        self.cli("-f", path, "down", "1", "1", "1.1")
        self.assertEqual(self.file.read_bytes(), original)

    def test_failed_commands_leave_file_untouched(self):
        path = str(self.file)
        self.assertIn("init", self.cli("-f", path, "list", ok=False))
        data = b"# Alpha\r\n## C\r\n- [ ] first\r\n## C\r\n- [x] second"
        self.file.write_bytes(data)
        for args in [
            ("init",), ("toggle", "Alpha", "C", "1"), ("delete", "1", "2", "0"),
            ("move", "1", "1", "1", "Unknown"), ("edit", "1", "1", "1", "a\nb"),
            ("toggle", "1", "1", "-1"), ("add", "2", "1", "third"),
        ]:
            with self.subTest(args=args):
                self.cli("--file", path, *args, ok=False)
                self.assertEqual(self.file.read_bytes(), data)
        self.cli("--file", path, "toggle", "1", "2", "1")
        self.assertEqual(self.file.read_bytes(), data.replace(b"[x]", b"[ ]"))

    def test_atomic_save_failure_cleans_up_without_truncation(self):
        source = BoardFile(self.file)
        source.create()
        original = self.file.read_bytes()
        doc = source.load().document
        doc = doc.add(doc.sections[0].columns[0], "new")
        with patch("kbtd.storage.os.replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                source.save(doc)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_replace_preserves_permissions_and_rereads(self):
        source = BoardFile(self.file)
        source.create()
        self.file.chmod(0o640)
        before = source.load()
        doc = before.document.add(before.document.sections[0].columns[0], "new")
        with patch.object(source, "load", wraps=source.load) as load:
            after = source.save(doc)
        load.assert_called_once_with()
        self.assertEqual(after.document, doc)
        self.assertNotEqual(before.version, after.version)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o640)

    def test_task_paths_and_fields_in_headless_cli(self):
        self.file.write_bytes(b"# TODO\r\n## Release\r\n- [ ] A\r\n  - [ ] Same\r\n"
                              b"- [x] B #docs\r\n  - [ ] Same\r\n    URL\r\n# DONE\r\n- [-] no")
        path = str(self.file)

        def run(cmd, *args):
            return self.cli("-f", path, cmd, "TODO", "Release", *args)

        run("owner", "2.1", "alice")
        self.assertIn("Same @alice", run("show", "2.1"))
        run("owner", "2.1", "bob")
        run("block", "2.1")
        self.assertEqual(run("show", "2.1"), "  - [ ] Same @bob #blocked\r\n    URL\r\n")
        run("owner", "2.1", "--clear")
        run("unblock", "2.1")
        run("edit", "2.1", "FAQ")
        run("toggle", "2.1")
        run("subtask", "2", "Another")
        run("subtask", "2.1", "Deep")
        self.assertIn("2.1.1. [ ] Deep", run("list"))
        self.assertIn("2.2. [ ] Another", run("list"))
        run("delete", "2.1")
        self.assertEqual(self.file.read_bytes(), b"# TODO\r\n## Release\r\n- [ ] A\r\n  - [ ] Same\r\n"
                         b"- [x] B #docs\r\n  - [ ] Another\r\n# DONE\r\n- [-] no")

    def test_ambiguous_metadata_bad_owners_and_paths_never_save(self):
        original = b"# TODO\n## C\n- [ ] T @a @b #blocked #blocked\n  - [ ] S"
        self.file.write_bytes(original)
        for cmd, path, args in [
            ("owner", "1", ["--clear"]), ("owner", "1.1", ["two words"]), ("owner", "1.1", ["@@x"]),
            ("block", "1", []), ("unblock", "1", []), ("move", "1.1", ["C"]),
            ("subtask", "1.2", ["wrong"]), ("delete", "1.0", []), ("toggle", "1..1", []),
        ]:
            with self.subTest(cmd=cmd, path=path, args=args):
                with patch.object(BoardFile, "save", side_effect=AssertionError("unexpected write")):
                    self.assertTrue(self.cli("-f", str(self.file), cmd, "1", "1", path, *args, ok=False))
                self.assertEqual(self.file.read_bytes(), original)

    def test_description_and_due_cli_on_subtask(self):
        original = b"# TODO\r\n## C\r\n- [ ] Parent\r\n  - [x] Child #docs\r\n    old note"
        self.file.write_bytes(original)

        def run(command, *values, ok=True):
            return self.cli("-f", str(self.file), command, "1", "C", "1.1", *values, ok=ok)

        self.assertEqual(run("due"), "")
        self.assertEqual(run("description"), "")
        run("description", "Day one\n\nDay two")
        run("due", "2028-02-29")
        self.assertEqual(run("due"), "2028-02-29\n")
        self.assertEqual(run("description"), "Day one\n\nDay two\n")
        self.assertIn("    Description: Day one\r\n    Description:\r\n    Description: Day two", run("show"))
        with patch.object(BoardFile, "save", side_effect=AssertionError("unexpected write")):
            for bad in ("2027-02-29", "2028-04-31", "20280229", "2028-W09-2", "2028-2-29"):
                self.assertIn("YYYY-MM-DD", run("due", bad, ok=False))
            run("due", "2028-02-29")
            run("description", "Day one\n\nDay two")
        run("due", "2028-03-01")
        run("description", "Done")
        run("due", "--clear")
        run("description", "--clear")
        self.assertEqual(self.file.read_bytes(), original)
        duplicate = original.replace(b"#docs", b"#docs #due-2028-01-01 #due-2029-01-01")
        self.file.write_bytes(duplicate)
        self.assertIn("ambiguous due date", run("due", "--clear", ok=False))
        self.assertEqual(self.file.read_bytes(), duplicate)

    def test_watch_registration_and_cli_edit_events(self):
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=str(self.file.parent / "state")))
        path = str(self.file)
        self.file.write_text("# Tasks\n## Work\n- [ ] Parent\n  - [ ] Child @sam #docs\n")
        self.cli("watch", "add", "record", "owner.changed", "exit 0", "-f", path)
        self.assertIn("record\towner.changed\texit 0", self.cli("-f", path, "watch", "list"))
        with patch("kbtd.storage.Watchers.emit_many") as emit:
            self.cli("-f", path, "edit", "Tasks", "Work", "1.1", "Child @lee #docs #review")
            self.assertEqual(emit.call_count, 1)
            [(kinds, event)] = emit.call_args.args[0]
            self.assertEqual(kinds, ("owner.changed", "tags.changed"))
            self.assertEqual((event["section"], event["column"], event["path"]), ("Tasks", "Work", "1.1"))
            self.assertEqual(event["before"]["owners"], ["sam"])
            self.assertEqual(event["after"]["owners"], ["lee"])
            self.assertEqual(event["after"]["tags"], ["docs", "review"])
            self.cli("-f", path, "edit", "Tasks", "Work", "1.1", "Reworded #review @lee #docs")
            self.cli("-f", path, "owner", "Tasks", "Work", "1.1", "lee")
            emit.assert_called_once()
        self.cli("-f", path, "watch", "remove", "record")
        self.assertIn("No watchers", self.cli("watch", "-f", path, "list"))

    def test_watchers_only_dispatch_after_save_and_report_queue_errors(self):
        self.file.write_text("# Tasks\n## Work\n- [ ] Intro\n")
        command = ("-f", str(self.file), "toggle", "1", "1", "1")
        with patch("kbtd.storage.Watchers.emit_many", side_effect=RuntimeError("queue unavailable")) as emit:
            with patch("kbtd.storage._atomic_write", side_effect=OSError("write failed")):
                self.assertIn("write failed", self.cli(*command, ok=False))
                emit.assert_not_called()
            self.assertIn("Board saved. Watcher dispatch failed", self.cli(*command, ok=False))
            self.assertIn("- [x] Intro", self.file.read_text())

    def test_moving_a_card_emits_a_status_change(self):
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=str(self.file.parent / "state")))
        os.environ.pop("KBTD_JOB_ID", None)
        self.enterContext(patch.object(Watchers, "_start_worker"))
        path = str(self.file)
        self.file.write_text("# Tasks\n## Backlog\n- [ ] Ship @sam\n  - [ ] Child\n## Done\n- [x] Old\n")
        for name, condition in [("into-done", 'task.status.changed_to == "Done"'),
                                ("out-of-backlog", 'task.status.changed_from == "Backlog"'),
                                ("any-move", "task.status.changed"), ("in-done", 'task.status == "Done"'),
                                ("into-backlog", 'task.status.changed_to == "Backlog"')]:
            self.cli("-f", path, "watch", "add", name, condition, "exit 0")
        watchers = Watchers(self.file)
        self.cli("-f", path, "move", "1", "1", "1", "Done")
        jobs = watchers.jobs()
        self.assertEqual(sorted(job["name"] for job in jobs), ["any-move", "in-done", "into-done", "out-of-backlog"])
        [job] = [job for job in jobs if job["name"] == "into-done"]
        event = watchers.job(job["id"])["event"]
        self.assertEqual((event["column"], event["column_number"], event["path"], event["events"]),
                         ("Done", 2, "2", ["task.status.changed"]))
        self.assertEqual((event["before"]["status"], event["after"]["status"], event["after"]["owners"]),
                         ("Backlog", "Done", ["sam"]))
        # No status change: a move within the column, a refused subtask move, an edit in Done.
        self.cli("-f", path, "move", "1", "2", "2", "Done")
        self.assertIn("top-level task", self.cli("-f", path, "move", "1", "2", "2.1", "Backlog", ok=False))
        self.cli("-f", path, "toggle", "1", "2", "2")
        names = [job["name"] for job in watchers.jobs()]
        self.assertEqual(len(names), 5)
        self.assertEqual(names[0], "in-done")

    def test_invalid_stored_trigger_is_reported_and_skipped(self):
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=str(self.file.parent / "state")))
        os.environ.pop("KBTD_JOB_ID", None)
        self.enterContext(patch.object(Watchers, "_start_worker"))
        path = str(self.file)
        self.file.write_text("# Tasks\n## Work\n- [ ] Intro\n")
        self.cli("-f", path, "watch", "add", "valid", "task.completed", "exit 0")
        watchers = Watchers(self.file)
        with watchers._database() as db:  # registered with the earlier TLA+-style syntax
            db.execute("INSERT INTO kbtd_watchers VALUES ('legacy', 'task.completed /\\ task.done', 'exit 0')")
        self.assertIn("Board saved. Skipped watcher 'legacy'", self.cli("-f", path, "toggle", "1", "1", "1", ok=False))
        self.assertIn("- [x] Intro", self.file.read_text())
        self.assertEqual([job["name"] for job in watchers.jobs()], ["valid"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["-f", path, "watch", "list"]), 0)
        self.assertIn("legacy\ttask.completed /\\ task.done", out.getvalue())
        self.assertIn("watcher 'legacy' never runs", err.getvalue())

    def test_conditional_watchers_match_once_per_saved_edit_on_the_selected_card(self):
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=str(self.file.parent / "state")))
        os.environ.pop("KBTD_JOB_ID", None)
        self.enterContext(patch.object(Watchers, "_start_worker"))
        path = str(self.file)
        self.file.write_text("# Tasks\n## Backlog\n- [ ] Parent\n  - [ ] Child\n## In Progress\n")
        watchers = Watchers(self.file)
        together = "owner.changed and tags.changed"
        either = '(owner.changed and "codex" in task.owners) or (tags.changed and "docs" in task.tags)'
        assigned = 'owner.changed and "codex" in task.owners and task.status == "Backlog"'
        for name, condition in [("both", together), ("either", either), ("assign", assigned),
                                ("z-owner", "owner.changed"), ("a-tags", "tags.changed")]:
            self.cli("-f", path, "watch", "add", name, condition, "exit 0")
        self.assertIn(assigned, self.cli("-f", path, "watch", "list"))
        self.assertIn("compare with ==", self.cli("-f", path, "watch", "add", "bad",
                                                  "task.status = Backlog", "exit 1", ok=False))

        # Neither different cards nor successive saves on a single card can
        # satisfy a conjunction of two events.
        self.cli("-f", path, "owner", "1", "1", "1", "someone")
        self.cli("-f", path, "block", "1", "1", "1.1")
        self.cli("-f", path, "owner", "1", "1", "1.1", "sam")
        self.cli("-f", path, "edit", "1", "1", "1.1", "Child @codex #docs")
        self.cli("-f", path, "owner", "1", "1", "1.1", "codex")  # no-op
        self.cli("-f", path, "move", "1", "1", "1", "In Progress")
        self.cli("-f", path, "owner", "1", "2", "1.1", "--clear")
        self.cli("-f", path, "owner", "1", "2", "1.1", "codex")
        jobs = list(reversed(watchers.jobs()))
        self.assertEqual([job["name"] for job in jobs], [
            "z-owner", "a-tags", "z-owner", "z-owner", "a-tags", "assign", "both", "either",
            "z-owner", "z-owner", "either",
        ])
        [both] = [job for job in jobs if job["name"] == "both"]
        shown = json.loads(self.cli("-f", path, "watch", "show", str(both["id"])))
        self.assertEqual((shown["name"], shown["status"], shown["command"]), ("both", "queued", "exit 0"))
        self.assertIn("no watcher job numbered 99", self.cli("-f", path, "watch", "show", "99", ok=False))
        # No worker ran, so the job is still in the outbox and cannot be cancelled yet.
        self.assertIn("has not reached the worker", self.cli("-f", path, "watch", "cancel", str(both["id"]), ok=False))
        event = shown["event"]
        self.assertEqual((event["events"], event["trigger"], event["column"], event["path"]),
                         (["owner.changed", "tags.changed"], together, "Backlog", "1.1"))
        self.assertEqual(event["after"]["owners"], ["codex"])
