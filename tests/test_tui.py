import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbtd.storage import BoardFile, SelectionStore

try:
    import curses
except ImportError as e:
    raise unittest.SkipTest("curses unavailable") from e

from kbtd.tui import BoardUI
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
        self.assertEqual((ui.card.text, ui.row), ("more", 1))
        ui.key("e")
        ui.key(curses.KEY_HOME)
        ui.key("S")
        ui.key("\n")
        self.assertEqual(ui.card.text, "Smore")
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
