"""Terminal rendering and hit testing. All hit boxes are rebuilt on each draw."""

from __future__ import annotations

import curses
import json
import textwrap
import unicodedata
from datetime import datetime
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


def _local(stamp: str, today_format: str = "%H:%M:%S", other_format: str = "%Y-%m-%d") -> str:
    """A job's UTC timestamp in local time."""
    try:
        moment = datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return stamp
    return moment.strftime(today_format if moment.date() == datetime.now().astimezone().date() else other_format)


class View:
    def __init__(self, screen: curses.window, muted: int = curses.A_DIM):
        self.screen, self.muted = screen, muted
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

    def _cards(self, column: Column, index: int, x: int, width: int, focus: int | None,
               show_dates: bool) -> None:
        capacity = self.height - 5

        def margin(i: int) -> str:
            indent = min(column.rows[i][1].indent, width - 7)
            return (" " * (indent - 1) + "↳") if indent else ""

        def wrap(i: int) -> list[str]:
            card = column.rows[i][1]
            dates = ""
            if show_dates and card.date_tokens:
                dates = "Due " + ", ".join(m[0].removeprefix("#due-") for m in card.date_tokens) + " · "
            label = ("BLOCKED · " if card.blocked else "") + dates + card.text_without_dates
            return textwrap.wrap(label, width=max(1, width - 5 - len(margin(i))),
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
        for i in range(first, len(column.rows)):
            _, card = column.rows[i]
            attr = self.muted if card.done else curses.A_NORMAL
            if card.blocked:
                attr |= curses.A_BOLD
            if i == focus:
                attr |= curses.A_REVERSE
            for j, line in enumerate(wrap(i)):
                prefix = "     " if j else (" [x] " if card.done else " [ ] ")
                self._put(y, x, ((" " * len(margin(i))) if j else margin(i)) + prefix + line,
                          attr, width, pad=i == focus)
                self.rows[index, y] = i
                y += 1
                if y >= self.height - 2:
                    return
        if not column.cards:
            self._put(y, x, " (empty)", self.muted, width)

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
                    self._put(y, left, "│", self.muted, 1)
                left += 1
            column = board.columns[i]
            attr = curses.A_BOLD | (curses.A_REVERSE if i == ui.col else curses.A_UNDERLINE)
            self._put(2, left, f" {i + 1}. {column.name} ({len(column.cards)})", attr, right - left, pad=True)
            self._cards(column, i, left, right - left, ui.row if i == ui.col else None, ui.show_dates)

    def _detail(self, ui: BoardUI) -> None:
        card = ui.card
        assert card is not None
        self._put(2, 0, "Task in TODO.md", curses.A_BOLD)
        lines = []
        for line in ui.doc.lines[card.start:card.end]:
            lines.extend(textwrap.wrap(line.rstrip("\r\n"), width=self.width,
                                       drop_whitespace=False) or [""])
        capacity = self.height - 5
        ui.detail_scroll = min(ui.detail_scroll, max(0, len(lines) - capacity))
        for y, line in enumerate(lines[ui.detail_scroll:ui.detail_scroll + capacity], 3):
            self._put(y, 0, line)

    def _jobs(self, ui: BoardUI) -> None:
        jobs = ui.jobs
        shown = jobs.shown
        if not jobs.all and jobs.error:
            self._put(2, 0, "Cannot read watcher jobs", curses.A_BOLD)
            self._put(3, 0, jobs.error)
            return
        if not shown:
            self._put(2, 0, "No watcher jobs yet. Register a watcher with 'kbtd watch add'."
                      if jobs.filter == "all" else f"No {jobs.filter} jobs.")
            return
        widths = [max(1, *(len(str(job["id"])) for job in shown)), 11,
                  min(16, max(7, *(len(job["name"]) for job in shown))), 0, 3, 10]
        # The trigger gets up to half of the space left after the fixed columns.
        free = self.width - sum(widths) - 2 * len(widths)
        widths[3] = max(7, min(max(len(job["trigger"]) for job in shown), free // 2))

        def line(cells: list[str]) -> str:
            return "  ".join(_clip(cell, width, pad=True) for cell, width in zip(cells, widths)) + "  " + cells[-1]

        self._put(2, 0, line(["#".rjust(widths[0]), "STATUS", "WATCHER", "TRIGGER", "TRY", "UPDATED", "DETAIL"]),
                  curses.A_BOLD | curses.A_UNDERLINE, pad=True)
        size = self.height - 5
        focus = jobs.index
        first = max(0, focus - size + 1)
        for y, i in enumerate(range(first, min(first + size, len(shown))), 3):
            job = shown[i]
            attr = (curses.A_BOLD if job["status"] in ("failed", "interrupted")
                    else self.muted if job["status"] in ("succeeded", "cancelled") else curses.A_NORMAL)
            if i == focus:
                attr |= curses.A_REVERSE
            self._put(y, 0, line([str(job["id"]).rjust(widths[0]), job["status"], job["name"], job["trigger"],
                                  str(job["attempt"]).rjust(3), _local(job["updated_at"]), job["detail"]]),
                      attr, pad=i == focus)

    def _job(self, ui: BoardUI) -> None:
        job = ui.jobs.detail
        assert job is not None
        self._put(2, 0, f"Job {job['id']} · {job['status']} · attempt {job['attempt']}", curses.A_BOLD)
        fields = [("Watcher", job["name"]), ("Trigger", job["trigger"]), ("Command", job["command"] or ""),
                  ("Created", _local(job["created_at"], "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")),
                  ("Updated", _local(job["updated_at"], "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")),
                  ("Exit code", "" if job["exit_code"] is None else str(job["exit_code"])),
                  ("Result", job["detail"])]
        source = [(f"{label:<10} {value}", 0) for label, value in fields if value]
        event = job["event"]
        source += [("", 0), ("Event", curses.A_BOLD)]
        source += [(text, 0) for text in (json.dumps(event, indent=2, ensure_ascii=False).splitlines()
                                          if event is not None else ["(not recorded)"])]
        source += [("", 0), ("Output (last 8 KiB)", curses.A_BOLD)]
        source += [(text, 0) for text in job["output"].splitlines() or ["(none)"]]
        lines = [(part, attr) for text, attr in source
                 for part in textwrap.wrap(text, width=self.width, drop_whitespace=False) or [""]]
        capacity = self.height - 6
        ui.detail_scroll = min(ui.detail_scroll, max(0, len(lines) - capacity))
        for y, (text, attr) in enumerate(lines[ui.detail_scroll:ui.detail_scroll + capacity], 4):
            self._put(y, 0, text, attr)

    def _job_status(self, ui: BoardUI) -> str:
        jobs = ui.jobs
        if jobs.error:
            return jobs.error
        counts = {}
        for job in jobs.all:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        newest = "newest " if len(jobs.all) >= jobs.limit else ""
        parts = [f"{len(jobs.all)} {newest}jobs"]
        parts += [f"{counts[status]} {status}" for status in ("running", "queued", "failed", "interrupted")
                  if counts.get(status)]
        if jobs.filter != "all":
            parts.append(f"showing {jobs.filter}")
        return " · ".join(parts)

    def _footer(self, ui: BoardUI) -> None:
        status = ui.notice
        if ui.mode in ("add", "edit", "subtask", "owner", "description", "due"):
            label = {"add": "New card: ", "edit": "Edit: ", "subtask": "New child: ",
                     "owner": "Owner: ", "description": "Description: ", "due": "Due: "}[ui.mode]
            # Keep the insertion point visible when editing a long line.
            display = ui.draft.replace("\n", "↵")
            before = _clip(display[:ui.caret][::-1], self.width - len(label) - 2)[::-1]
            self._put(self.height - 2, 0, label + before + "▏" + display[ui.caret:], curses.A_BOLD)
            hints = "Enter: save   Esc: cancel   ←/→: edit"
            if ui.mode == "description":
                hints += "   Ctrl-N: new line"
            hints = status or hints
        else:
            if ui.mode == "delete":
                status = ("Delete subtree AND notes? (y/N)" if ui.card and ui.card.children
                          else "Delete task, subtasks and notes? (y/N)")
                hints = "y: delete   any other key: cancel"
            elif ui.mode == "detail":
                hints = "↑/↓: scroll   Esc: back   q: quit"
            elif ui.mode == "jobs":
                status = status or self._job_status(ui)
                hints = "↑/↓: job   Enter: details   r: retry   c: cancel   g: go to card   f: filter   Esc: back"
            elif ui.mode == "job":
                status = status or self._job_status(ui)
                hints = "↑/↓ PgUp/PgDn: scroll   r: retry   c: cancel   g: go to card   Esc: back   q: quit"
            elif ui.mode == "help":
                hints = "Press any key to return   q: quit"
            elif ui.section is None or ui.read_error:
                hints = "↑/↓: board   Enter: open   w: jobs   ?: help   q: quit"
            else:
                if not status and ui.column and ui.board:
                    status = f"Column {ui.col + 1}/{len(ui.board.columns)} · {ui.column.name}"
                    if ui.card:
                        title = ui.card.text if ui.show_dates else ui.card.text_without_dates
                        status += f" · Task {ui.column.rows[ui.row][0]}: {title}"
                hints = "?: help   q: quit   w: jobs   arrows: focus   Space: toggle   K/J: priority   H/L: column   a/e/d: add/edit/delete"
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
            if ui.mode in ("jobs", "job"):
                title += "  /  Jobs"
            self._put(0, 0, title, curses.A_BOLD)
            self._put(1, 0, str(ui.source.path), self.muted)
            if ui.mode == "help":
                for y, line in enumerate((
                    "Keyboard & mouse", "↑/↓ or k/j    Select board or card",
                    "←/→ or h/l    Select column", "Enter         Open board or toggle card",
                    "Space / click Toggle completion", "H/L or </>    Move card one column left/right",
                    "K/J           Move task up/down among siblings",
                    "Drag & drop   Append card to another column", "a / e / d     Add, edit or delete card",
                    "s             Add subtask to selection", "o             Owner (empty clears)",
                    "B             Toggle #blocked", "i / D         Inspect task / edit description",
                    "u / v         Edit due date / show or hide dates",
                    "w             Watcher jobs: Enter details, r retry, c cancel,",
                    "              g go to card, f filter, Esc back",
                    "b / Esc       Back to board picker", "q             Quit",
                ), 2):
                    if y < self.height - 2:
                        self._put(y, 0, line, curses.A_BOLD if y == 2 else 0)
            elif ui.mode == "jobs":
                self._jobs(ui)
            elif ui.mode == "job":
                self._job(ui)
            elif ui.mode == "detail":
                self._detail(ui)
            elif ui.read_error:
                self._put(2, 0, "Cannot read file; waiting for it to become available.", curses.A_BOLD)
                self._put(3, 0, ui.read_error)
            elif ui.board is None:
                self._picker(ui)
            else:
                self._board(ui)
            self._footer(ui)
        self.screen.refresh()
