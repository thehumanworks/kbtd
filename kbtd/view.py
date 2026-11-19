"""Terminal rendering and hit testing. All hit boxes are rebuilt on each draw."""

from __future__ import annotations

import curses
import textwrap
import unicodedata
from typing import TYPE_CHECKING

from .markdown import Column

if TYPE_CHECKING:
    from .tui import BoardUI


def _clip(text: str, width: int, pad: bool = False) -> str:
    """Clip in terminal cells, not code points; never emit terminal controls."""
    chars: list[str] = []
    used = 0
    for ch in text:
        if not ch.isprintable():
            ch = " "
        size = 0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + size > width:
            if used < width:
                chars.append("…")
                used += 1
            break
        chars.append(ch)
        used += size
    return "".join(chars) + (" " * (width - used) if pad else "")


class View:
    def __init__(self, screen: curses.window, accent: int = curses.A_BOLD):
        self.screen, self.accent = screen, accent
        self.picks: dict[int, int] = {}
        self.lanes: list[tuple[int, int, int]] = []  # left, right (exclusive), column
        self.rows: dict[tuple[int, int], int] = {}  # (column, screen y) -> card

    @property
    def too_small(self) -> bool:
        return self.height < 7 or self.width < 18

    def column_at(self, x: int, y: int) -> int | None:
        if 2 <= y < self.height - 2:
            return next((col for left, right, col in self.lanes if left <= x < right), None)
        return None

    def card_at(self, x: int, y: int) -> tuple[int, int] | None:
        col = self.column_at(x, y)
        if col is not None and (row := self.rows.get((col, y))) is not None:
            return col, row
        return None

    def _put(self, y: int, x: int, text: str, attr: int = 0, width: int | None = None, pad: bool = False) -> None:
        space = self.width - x if width is None else min(width, self.width - x)
        if 0 <= y < self.height and x >= 0 and space > 0:
            try:
                self.screen.addstr(y, x, _clip(text, space, pad), attr)
            except curses.error:
                pass  # Resize race, or ncurses reporting a write at the bottom-right cell.

    def _picker(self, ui: BoardUI) -> None:
        self._put(2, 0, "Choose a board", curses.A_BOLD)
        size = self.height - 5
        first = max(0, ui.pick - size + 1)
        for y, i in enumerate(range(first, min(first + size, len(ui.doc.sections))), 3):
            self.picks[y] = i
            section = ui.doc.sections[i]
            self._put(y, 0, f" {i + 1}. {section.name}  ({len(section.columns)} columns)",
                      curses.A_REVERSE if i == ui.pick else 0, pad=True)
        if not ui.doc.sections:
            self._put(3, 0, "No boards yet. Add a '# Section' and '## Column' in the file.")

    def _cards(self, column: Column, index: int, x: int, width: int, focus: int | None) -> None:
        capacity = self.height - 5

        def wrap(i: int) -> list[str]:
            return textwrap.wrap(column.cards[i].text, width=max(1, width - 5),
                                 max_lines=min(3, capacity), placeholder="…") or [""]

        # Scroll by whole cards, keeping all the focused card's rows on screen.
        first = focus or 0
        if first:
            room = capacity - len(wrap(first))
            while first > 0:
                n = len(wrap(first - 1))
                if n > room:
                    break
                room -= n
                first -= 1
        y = 3
        for i in range(first, len(column.cards)):
            card = column.cards[i]
            attr = curses.A_DIM if card.done else curses.A_NORMAL
            if i == focus:
                attr |= curses.A_REVERSE
            for j, line in enumerate(wrap(i)):
                prefix = "     " if j else (" [x] " if card.done else " [ ] ")
                self._put(y, x, prefix + line, attr, width, pad=i == focus)
                self.rows[index, y] = i
                y += 1
                if y >= self.height - 2:
                    return
        if not column.cards:
            self._put(y, x, " (empty)", curses.A_DIM, width)

    def _board(self, ui: BoardUI) -> None:
        board = ui.board
        assert board is not None
        if not board.columns:
            self._put(3, 0, "No columns. Add a '## Column' under this section in the file.")
            return
        slots = min(len(board.columns), max(1, self.width // 22))
        first = min(max(0, ui.col - slots // 2), len(board.columns) - slots)
        for j in range(slots):
            i = first + j
            left, right = self.width * j // slots, self.width * (j + 1) // slots
            self.lanes.append((left, right, i))
            if j:
                for y in range(2, self.height - 2):
                    self._put(y, left, "│", curses.A_DIM, 1)
                left += 1
            column = board.columns[i]
            attr = self.accent | (curses.A_REVERSE if i == ui.col else curses.A_UNDERLINE)
            self._put(2, left, f" {i + 1}. {column.name} ({len(column.cards)})", attr, right - left, pad=True)
            self._cards(column, i, left, right - left, ui.row if i == ui.col else None)

    def _footer(self, ui: BoardUI) -> None:
        status = ui.notice
        if ui.mode in ("add", "edit"):
            label = "New card: " if ui.mode == "add" else "Edit: "
            # Keep the insertion point visible when editing a long line.
            before = _clip(ui.draft[:ui.caret][::-1], self.width - len(label) - 2)[::-1]
            self._put(self.height - 2, 0, label + before + "▏" + ui.draft[ui.caret:], curses.A_BOLD)
            hints = status or "Enter: save   Esc: cancel   ←/→: edit"
        else:
            if ui.mode == "delete":
                status = "Delete this card AND its indented notes? (y/N)"
                hints = "y: delete   any other key: cancel"
            elif ui.mode == "help":
                hints = "Press any key to return   q: quit"
            elif ui.section is None or ui.read_error:
                hints = "↑/↓: board   Enter: open   ?: help   q: quit"
            else:
                if not status and ui.column and ui.board:
                    status = f"Column {ui.col + 1}/{len(ui.board.columns)} · {ui.column.name}"
                    if ui.card:
                        status += f" · Card {ui.row + 1}/{len(ui.column.cards)}: {ui.card.text}"
                hints = "?: help   q: quit   b: boards   arrows: focus   Space: toggle   H/L: move   a/e/d: add/edit/delete"
            self._put(self.height - 2, 0, status, curses.A_BOLD)
        self._put(self.height - 1, 0, hints, curses.A_REVERSE, pad=True)

    def draw(self, ui: BoardUI) -> None:
        self.height, self.width = self.screen.getmaxyx()
        self.picks.clear()
        self.lanes.clear()
        self.rows.clear()
        self.screen.erase()
        if self.too_small:
            self._put(0, 0, "Resize terminal (18×7 min); q quits")
        else:
            title = f"[Boards]  /  {ui.board.name}" if ui.board else "KBTD  /  Boards"
            self._put(0, 0, title, self.accent)
            self._put(1, 0, str(ui.source.path), curses.A_DIM)
            if ui.mode == "help":
                for y, line in enumerate((
                    "Keyboard & mouse", "↑/↓ or k/j    Select board or card",
                    "←/→ or h/l    Select column", "Enter         Open board or toggle card",
                    "Space / click Toggle completion", "H/L or </>    Move card one column left/right",
                    "Drag & drop   Append card to another column", "a / e / d     Add, edit or delete card",
                    "b / Esc       Back to board picker", "q             Quit",
                ), 2):
                    if y < self.height - 2:
                        self._put(y, 0, line, curses.A_BOLD if y == 2 else 0)
            elif ui.read_error:
                self._put(2, 0, "Cannot read file; waiting for it to become available.", curses.A_BOLD)
                self._put(3, 0, ui.read_error)
            elif ui.board is None:
                self._picker(ui)
            else:
                self._board(ui)
            self._footer(ui)
        self.screen.refresh()
