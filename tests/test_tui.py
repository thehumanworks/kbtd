import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from kbtd.cli import main
from kbtd.storage import BoardFile, SelectionStore
from kbtd.watchers import Watchers

try:
    import curses
except ImportError as e:
    raise unittest.SkipTest("curses unavailable") from e

from kbtd.tui import BoardUI, _apply_theme, _locate
from kbtd.view import View


class Screen:
    """Recording terminal; no tty or curses initialization needed."""

    def __init__(self, height=18, width=90):
        self.size = height, width
        self.entries = {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.entries.clear()

    def addstr(self, y, x, text, attr):
        self.entries[y, x] = text, attr

    def refresh(self):
        pass


class TuiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=str(root / "state")))
        self.source = BoardFile(root / "TODO.md")
        self.source.path.write_text(
            "# Alpha\n## Queue\n- [ ] first\n  hidden notes\n- [x] second\n## Doing\n## Done\n"
            "# Beta\n## Ideas\n- [ ] think\n"
        )
        self.ui = self.open()
        self.screen = Screen()
        self.view = View(self.screen)

    def open(self):
        return BoardUI(self.source, self.source.load())

    def test_theme_selection_at_tui_launch(self):
        with (patch("sys.stdin.isatty", return_value=True),
              patch("sys.stdout.isatty", return_value=True),
              patch("kbtd.tui.run_tui") as launch,
              patch.dict(os.environ)):
            os.environ.pop("KBTD_THEME", None)
            path = str(self.source.path)
            for args, env, expected in [
                ([path], "", "system"),
                (["-f", path], "vercel-dark", "vercel-dark"),
                (["tui", path, "--theme", "vercel-light"], "vercel-dark", "vercel-light"),
            ]:
                if env:
                    os.environ["KBTD_THEME"] = env
                self.assertEqual(main(args), 0)
                self.assertEqual(launch.call_args.args[-1], expected)

    def test_watcher_events_from_tui_edits(self):
        self.ui.select(0)
        with patch("kbtd.storage.Watchers.emit_many") as emit:
            for key in "oamy\nBu2028-02-29\n  ":
                self.ui.key(key)
            events = [call.args[0][0] for call in emit.call_args_list]
            self.assertEqual([kinds for kinds, _ in events], [
                ("owner.changed",), ("tags.changed",), ("tags.changed",), ("task.completed",), ("task.reopened",)
            ])
            self.assertEqual(events[2][1]["after"]["tags"], ["blocked", "due-2028-02-29"])
            self.assertTrue(events[3][1]["after"]["done"])
            self.assertFalse(events[4][1]["after"]["done"])
            emit.reset_mock()
            self.ui.key("J")
            self.source.path.write_text(self.source.path.read_text().replace("@amy", "@bob"))
            self.assertTrue(self.ui.poll())
            emit.assert_not_called()

    def test_moves_by_key_and_drag_emit_status_changes(self):
        ui, view = self.ui, self.view
        with patch("kbtd.storage.Watchers.emit_many") as emit:
            view.draw(ui)
            ui.mouse(view, 0, 3, curses.BUTTON1_CLICKED)
            view.draw(ui)
            ui.mouse(view, 2, 3, curses.BUTTON1_PRESSED)
            ui.mouse(view, 82, 8, curses.BUTTON1_RELEASED)  # drag "first" into Done
            ui.key("H")
            ui.key("H")
            ui.key("H")  # already in the first column: no move
            ui.key("J")  # reordering is not a status change
            events = [call.args[0][0] for call in emit.call_args_list]
        self.assertEqual([kinds for kinds, _ in events], [("task.status.changed",)] * 3)
        self.assertEqual([(p["before"]["status"], p["after"]["status"], p["column"], p["column_number"])
                          for _, p in events],
                         [("Queue", "Done", "Done", 3), ("Done", "Doing", "Doing", 2), ("Doing", "Queue", "Queue", 1)])
        # The path names the task's place after the move; Queue appends it after "second".
        self.assertEqual([(p["after"]["text"], p["path"]) for _, p in events],
                         [("first", "1"), ("first", "1"), ("first", "2")])

    def test_conditional_watcher_follows_tui_edits_across_columns(self):
        os.environ.pop("KBTD_JOB_ID", None)  # setUp isolates the environment
        watchers = Watchers(self.source.path)
        condition = '"codex" in owner.added and tags.changed and task.status == "Queue"'
        watchers.add("combined", condition, "exit 0")
        self.enterContext(patch.object(Watchers, "_start_worker"))
        self.ui.select(0)
        for key in "e @codex #docs\n":
            self.ui.key(key)
        for key in "e @sam #review\n":  # Both owner and tag sets change while Codex stays assigned.
            self.ui.key(key)
        self.ui.key("L")  # Repeat an edit that changes both fields in Doing.
        for title in ("first @sam #old", "first @codex #docs"):
            self.ui.key("e")
            for _ in self.ui.draft:
                self.ui.key(curses.KEY_BACKSPACE)
            for key in title + "\n":
                self.ui.key(key)
        self.assertEqual((self.ui.column.name, self.ui.card.text), ("Doing", "first @codex #docs"))
        self.assertEqual([job["trigger"] for job in watchers.jobs()], [condition])

    def screen_text(self):
        self.view.draw(self.ui)
        return "\n".join(text for _, (text, _) in sorted(self.screen.entries.items()))

    def test_jobs_view_lists_filters_acts_and_jumps_to_the_card(self):
        stamp = "2026-01-02T03:04:05.000Z"
        failed = {"id": 2, "name": "notify", "trigger": 'owner.changed = "codex"', "status": "failed",
                  "attempt": 1, "detail": "exit code 9", "created_at": stamp, "updated_at": stamp}
        done = {**failed, "id": 1, "status": "succeeded", "detail": "exit code 0"}
        # The recorded path is stale: "second" was the first card when the job ran.
        event = {"section": "Alpha", "section_number": 1, "column": "Queue", "column_number": 1,
                 "path": "1", "after": {"text": "second"}}
        detail = {**failed, "command": "./notify.sh", "event": event, "exit_code": 9, "output": "boom\n"}
        listing = self.enterContext(patch.object(Watchers, "jobs", return_value=[failed, done]))
        self.enterContext(patch.object(Watchers, "job", return_value=detail))
        retry = self.enterContext(patch.object(Watchers, "retry"))
        cancel = self.enterContext(patch.object(Watchers, "cancel",
                                                side_effect=ValueError("only queued watcher jobs can be cancelled")))
        ui = self.ui
        ui.key("w")
        text = self.screen_text()
        self.assertIn("KBTD  /  Boards  /  Jobs", text)
        self.assertIn("2 jobs", text)
        self.assertIn("exit code 9", text)
        self.assertEqual(ui.jobs.current["id"], 2)

        ui.key("j")
        self.assertEqual(ui.jobs.current["id"], 1)
        listing.return_value = [{**failed, "id": 3, "status": "queued"}, failed, done]
        self.assertTrue(ui.jobs.refresh(force=True))
        self.assertEqual(ui.jobs.current["id"], 1)  # a new job does not move the selection
        ui.key("f")
        self.assertEqual((ui.jobs.filter, [j["id"] for j in ui.jobs.shown], ui.jobs.current["id"]),
                         ("failed", [2], 2))
        self.assertIn("showing failed", self.screen_text())
        ui.key("r")
        retry.assert_called_once_with(2)
        self.assertEqual(ui.notice, "Queued job 2 for retry")
        ui.key("c")
        cancel.assert_called_once_with(2)
        self.assertIn("only queued", self.screen_text())

        self.source.path.write_text(self.source.path.read_text().replace("first", "first!"))
        self.assertTrue(ui.poll())
        self.assertEqual(ui.mode, "jobs")  # a board reload keeps the job view open
        ui.key("\n")
        text = self.screen_text()
        self.assertEqual(ui.mode, "job")
        for expected in ("Job 2 · failed · attempt 1", "./notify.sh", "Exit code  9"):
            self.assertIn(expected, text)
        self.assertNotIn("boom", text)
        ui.key(curses.KEY_NPAGE)
        text = self.screen_text()
        for expected in ('"path": "1"', "Output (last 8 KiB)", "boom"):
            self.assertIn(expected, text)
        ui.key("?")
        ui.key("x")
        self.assertEqual(ui.mode, "job")

        ui.key("g")
        self.assertEqual((ui.mode, ui.board.name, ui.column.name, ui.card.text),
                         ("browse", "Alpha", "Queue", "second"))
        self.assertEqual(ui.notice, "Card from job 2")
        detail["event"] = {**event, "after": {"text": "renamed"}}
        ui.key("w")
        ui.key("g")
        self.assertEqual((ui.mode, ui.card.text), ("browse", "first!"))
        self.assertIn("text changed", ui.notice)
        detail["event"] = {**event, "section": "Gone"}
        ui.key("w")
        ui.key("g")
        self.assertEqual(ui.mode, "jobs")
        self.assertIn("no longer on this board", ui.notice)
        ui.key("\x1b")
        self.assertEqual((ui.mode, ui.board.name), ("browse", "Alpha"))

    def test_jump_to_card_prefers_the_card_id(self):
        from kbtd.markdown import parse_markdown
        doc = parse_markdown("<!-- kbtd next-id: KBT-0003 -->\n# Alpha\n## Queue\n- [ ] KBT-0002 renamed\n"
                             "- [ ] KBT-0001 old title\n## Done\n- [ ] old title\n")
        # The card was renamed and moved after the job; its ID still finds it.
        event = {"section": "Alpha", "section_number": 1, "column": "Done", "column_number": 2,
                 "path": "1", "after": {"id": "KBT-0002", "text": "old title"}}
        self.assertEqual(_locate(doc, event), ((0, 0, 0), False))
        self.assertEqual(_locate(doc, {**event, "after": {"text": "old title"}}), ((0, 1, 0), True))

    def test_jobs_view_reports_empty_history_and_errors(self):
        jobs = self.enterContext(patch.object(Watchers, "jobs", return_value=[]))
        self.ui.key("w")
        self.assertIn("No watcher jobs yet", self.screen_text())
        self.ui.key("f")
        self.assertIn("No failed jobs.", self.screen_text())
        for key in ("r", "c", "g", "\n"):
            self.ui.key(key)
        self.assertEqual(self.ui.mode, "jobs")
        jobs.side_effect = RuntimeError("watcher jobs need the dbos package")
        self.assertTrue(self.ui.jobs.refresh(force=True))
        self.assertIn("need the dbos package", self.screen_text())
        self.ui.key("w")
        self.assertEqual(self.ui.mode, "browse")

    def test_theme_palettes_and_terminal_fallbacks(self):
        screen = Mock()
        with patch("curses.has_colors", return_value=False):
            self.assertEqual(_apply_theme(screen, "vercel-light"), curses.A_DIM)
        screen.bkgd.assert_not_called()
        with (patch("curses.has_colors", return_value=True),
              patch("curses.use_default_colors") as defaults,
              patch("curses.color_pair", side_effect=lambda n: n << 8),
              patch("curses.init_pair") as init):
            self.assertEqual(_apply_theme(screen, "system"), curses.A_DIM)
            defaults.assert_called_once()
            init.assert_not_called()
            for theme, fg, bg, muted in [("vercel-light", 16, 231, 241), ("vercel-dark", 231, 16, 247)]:
                with patch("curses.COLORS", 256, create=True):
                    self.assertEqual(_apply_theme(screen, theme), 2 << 8)
                    self.assertEqual(init.call_args_list, [call(1, fg, bg), call(2, muted, bg)])
                    screen.bkgd.assert_called_with(" ", 1 << 8)
                    init.reset_mock()
            with patch("curses.COLORS", 8, create=True):
                self.assertEqual(_apply_theme(screen, "vercel-light"), curses.A_DIM)
                init.assert_called_once_with(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
                init.side_effect = curses.error
                self.assertEqual(_apply_theme(screen, "vercel-dark"), curses.A_DIM)

    def test_picker_board_and_keyboard_edits(self):
        ui = self.ui
        self.assertIsNone(ui.board)
        ui.key("\n")
        self.view.draw(ui)
        self.assertIn("Alpha", self.screen.entries[0, 0][0])
        titles = [text for (y, _), (text, _) in self.screen.entries.items() if y == 2]
        for name in ("Queue", "Doing", "Done"):
            self.assertTrue(any(name in title for title in titles))
        self.assertFalse(any("hidden notes" in text for text, _ in self.screen.entries.values()))
        self.assertTrue(self.screen.entries[4, 0][1] & curses.A_DIM)
        ui.key(" ")
        ui.key("L")
        self.assertEqual((ui.column.name, ui.row, ui.card.done), ("Doing", 0, True))
        self.assertIn("## Doing\n- [x] first\n  hidden notes", self.source.path.read_text())
        ui.key("a")
        for ch in "more":
            ui.key(ch)
        ui.key("\n")
        # The temporary folder is not a git repository: tmpXXXX gives TMP.
        self.assertEqual((ui.card.text, ui.row, ui.notice), ("TMP-0001 more", 1, "Added TMP-0001"))
        ui.key("e")
        self.view.draw(ui)
        self.assertEqual((ui.draft, self.screen.entries[16, 0][0][:16]), ("more", "Edit TMP-0001: m"))
        ui.key(curses.KEY_HOME)
        ui.key("S")
        ui.key("\n")
        self.assertEqual(ui.card.text, "TMP-0001 Smore")
        ui.key("d")
        ui.key("n")
        self.assertEqual(len(ui.column.cards), 2)
        ui.key("d")
        ui.key("y")
        self.assertEqual((ui.card.text, ui.row), ("first", 0))
        self.assertEqual(ui.doc, self.source.load().document)
        ui.key("b")
        self.assertIsNone(ui.board)
        self.assertFalse(ui.key("q"))

    def test_priority_keys_keep_focus_on_the_moved_subtree(self):
        original = ("# Alpha\n## Queue\n- [ ] Parent\n  - [ ] First\n  - [ ] Second\n"
                    "    - [ ] Grandchild\n- [x] Next\n  - [ ] Child of next")
        self.source.path.write_text(original)
        ui = self.open()
        ui.select(0)
        ui.key("J")
        self.assertEqual((ui.column.rows[ui.row][0], ui.card.text, ui.row), ("2", "Parent", 2))
        ui.key("j")
        ui.key("J")
        self.assertEqual((ui.column.rows[ui.row][0], ui.card.text, ui.row), ("2.2", "First", 5))
        with patch.object(self.source, "save", side_effect=AssertionError("boundary move wrote to disk")):
            ui.key("J")
        self.assertEqual((ui.card.text, ui.row), ("First", 5))
        ui.key("K")
        ui.key("k")
        ui.key("K")
        self.assertEqual((ui.card.text, ui.row), ("Parent", 0))
        self.assertEqual(self.source.path.read_text(), original)

    def test_click_and_drag(self):
        ui, view = self.ui, self.view
        view.draw(ui)
        ui.mouse(view, 0, 3, curses.BUTTON1_CLICKED)
        self.assertEqual(ui.board.name, "Alpha")
        view.draw(ui)
        ui.mouse(view, 3, 3, curses.BUTTON1_PRESSED)
        ui.mouse(view, 3, 3, curses.BUTTON1_RELEASED)
        self.assertTrue(ui.card.done)
        view.draw(ui)
        ui.mouse(view, 2, 3, curses.BUTTON1_PRESSED)
        ui.mouse(view, 82, 8, curses.BUTTON1_RELEASED)  # drop into empty space in Done
        self.assertEqual((ui.column.name, ui.card.text, ui.card.done), ("Done", "first", True))
        self.assertIn("## Done\n- [x] first\n  hidden notes", self.source.path.read_text())
        ui.mouse(view, 4, 0, curses.BUTTON1_CLICKED)
        self.assertIsNone(ui.board)

    def test_edit_description_due_and_toggle_date_visibility(self):
        ui = self.ui
        ui.key("\n")
        original = self.source.path.read_bytes()

        def replace(key, value):
            ui.key(key)
            for _ in ui.draft:
                ui.key(curses.KEY_BACKSPACE)
            for ch in value:
                ui.key("\x0e" if ch == "\n" else ch)
            ui.key("\n")

        replace("D", "First\nSecond")
        ui.key("i")
        self.view.draw(ui)
        self.assertTrue(any("Description: Second" in text for text, _ in self.screen.entries.values()))
        ui.key("\x1b")
        replace("D", "Replacement")
        ui.key("u")
        for ch in "2027-02-29\n":
            ui.key(ch)
        self.assertEqual(ui.mode, "due")
        self.assertIn("YYYY-MM-DD", ui.notice)
        self.assertNotIn(b"#due-", self.source.path.read_bytes())
        ui.key("\x1b")
        replace("u", "2028-02-29")
        replace("u", "2028-03-01")
        with patch.object(self.source, "save", side_effect=AssertionError("display must not save")):
            for key, visible in [("", True), ("v", False), ("v", True)]:
                ui.key(key)
                self.view.draw(ui)
                self.assertEqual(any("2028-03-01" in t for t, _ in self.screen.entries.values()), visible)
        replace("D", "")
        replace("u", "")
        self.assertEqual(self.source.path.read_bytes(), original)

    def test_restore_by_path_and_name_not_section_index(self):
        ui = self.ui
        ui.key("j")
        ui.key("\n")
        self.assertEqual(self.open().board.name, "Beta")
        other = self.source.path.parent / "other" / "TODO.md"
        self.assertIsNone(SelectionStore(other).load())
        ui.key("e")
        ui.key("!")
        self.source.path.write_text("# Beta\n## New\n- [ ] external\n# Alpha\n")
        self.assertTrue(ui.poll())
        self.assertEqual((ui.section, ui.card.text, ui.mode), (0, "external", "browse"))
        self.assertFalse(ui.poll())
        self.source.path.write_text("# Alpha\n")
        self.assertTrue(ui.poll())
        self.assertIsNone(ui.board)
        self.assertIsNone(self.open().board)

    def test_temporarily_unavailable_or_invalid_file_is_not_overwritten(self):
        ui = self.ui
        ui.key("\n")
        self.source.path.unlink()
        self.assertTrue(ui.poll())
        self.assertTrue(ui.read_error)
        ui.key(" ")
        self.assertFalse(self.source.path.exists())
        self.source.path.write_bytes(b"\xff")
        ui.poll()
        ui.key("L")
        self.assertEqual(self.source.path.read_bytes(), b"\xff")
        self.source.path.write_text("# Alpha\n## Empty\n")
        self.assertTrue(ui.poll())
        self.assertFalse(ui.read_error)
        self.assertEqual(ui.column.name, "Empty")

    def test_scroll_keeps_focused_column_and_card_visible(self):
        text = "# S\n" + "".join(f"## C{i}\n" + "- [ ] long title wrapped across rows\n" * 8 for i in range(6))
        self.source.path.write_text(text)
        ui = self.open()
        ui.key("\n")
        self.screen.size = 10, 45
        for _ in range(9):
            ui.key("l")
            ui.key("j")
        self.view.draw(ui)
        self.assertEqual((ui.col, ui.row), (5, 7))
        self.assertEqual(len(self.view.lanes), 2)
        self.assertIn((5, 7), [self.view.card_at(40, y) for y in range(3, 8)])

    def test_empty_board_and_read_only_or_corrupt_ui_state_are_usable(self):
        self.source.path.write_text("# Empty\n")
        state = SelectionStore(self.source.path)
        state.save(("gone", 0))
        state.path.write_text("bad json")
        ui = self.open()
        with patch("kbtd.storage._atomic_write", side_effect=PermissionError):
            ui.key("\n")
        self.assertEqual(ui.board.name, "Empty")
        for key in ["j", "l", "L", "a", " ", "d"]:
            ui.key(key)
        self.view.draw(ui)
        self.assertIn("No columns", self.screen.entries[3, 0][0])
        self.source.path.write_text("No headings at all.")
        ui.poll()
        self.view.draw(ui)
        self.assertIn("No boards", self.screen.entries[3, 0][0])

    def test_readme_board_owner_block_hierarchy_and_delete_across_interfaces(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        example = readme.split("## File format\n", 1)[1].split("```markdown\n", 1)[1].split("```", 1)[0]
        self.source.path.write_text(example)
        ui = self.open()
        ui.key("\n")
        ui.key("o")
        self.assertEqual(ui.draft, "alice")
        for _ in "alice":
            ui.key(curses.KEY_BACKSPACE)
        for char in "bob\n":
            ui.key(char)
        self.assertEqual(self.source.path.read_text(), example.replace("@alice", "@bob"))
        ui.key("B")
        self.view.draw(ui)
        self.assertIn("BLOCKED", self.screen.entries[3, 0][0])
        self.assertTrue(self.screen.entries[3, 0][1] & curses.A_BOLD)
        child_y = next(y for (y, x), (text, _) in self.screen.entries.items() if x == 0 and "↳" in text)
        self.assertEqual(self.view.card_at(3, child_y), (0, 1))
        ui.key("i")
        self.view.draw(ui)
        self.assertTrue(any("Keep links relative" in s for s, _ in self.screen.entries.values()))
        ui.key("\x1b")
        ui.key("B")
        ui.key("o")
        for _ in "bob":
            ui.key(curses.KEY_BACKSPACE)
        ui.key("\n")
        ui.key("o")
        for ch in "amy\n":
            ui.key(ch)
        self.assertEqual(self.source.path.read_text(), example.replace("@alice #docs", "#docs @amy"))

        ui.key("s")
        for ch in "Screenshots\n":
            ui.key(ch)
        self.assertEqual(ui.card.text, "Screenshots")
        self.assertEqual(ui.column.rows[ui.row][0], "1.2")
        ui.key("e")
        ui.key("!")
        ui.key("\n")
        self.assertEqual(ui.card.text, "Screenshots!")
        ui.key("d")
        ui.key("y")
        self.assertNotIn("Screenshots", self.source.path.read_text())

        self.assertEqual(main(["-f", str(self.source.path), "edit", "TODO", "Release", "2.1", "FAQ links"]), 0)
        self.assertTrue(ui.poll())
        ui.select(0)
        ui.key("d")
        self.view.draw(ui)
        self.assertIn("subtree", self.screen.entries[16, 0][0])
        before = self.source.path.read_bytes()
        with patch.object(self.source, "save", side_effect=AssertionError("cancel must not save")):
            ui.key("n")
        self.assertEqual(self.source.path.read_bytes(), before)
        ui.key("d")
        ui.key("y")
        self.assertEqual(self.source.path.read_text(), example[:example.index("- [ ] Publish guide")]
                         + example[example.index("- [ ] Publish FAQ"):].replace("Review links", "FAQ links"))

    def test_new_actions_cancel_on_reload_and_moves_preserve_the_subtree(self):
        self.source.path.write_text("# TODO\n## A\n- [ ] Parent\n  - [x] Kid\n## B\n- [ ] Other\n  - [ ] Kid\n")
        ui = self.open()
        ui.select(0)
        ui.key("j")
        ui.key("L")
        self.assertIn("top-level", ui.notice)
        self.assertEqual((ui.column.name, ui.card.text), ("A", "Kid"))
        ui.key("k")
        ui.key("L")
        self.assertEqual((ui.column.name, ui.row, ui.card.text), ("B", 2, "Parent"))
        self.assertEqual(ui.column.rows[-1][0], "2.1")
        self.assertTrue(ui.column.rows[-1][1].done)
        for key, mode in [("o", "owner"), ("s", "subtask"), ("d", "delete")]:
            ui.key(key)
            self.assertEqual(ui.mode, mode)
            updated = self.source.path.read_text().replace("Parent", "Parent!")
            self.source.path.write_text(updated)
            self.assertTrue(ui.poll())
            self.assertEqual(ui.mode, "browse")
            self.assertEqual(self.source.path.read_text(), updated)
