"""Curses event loop and interaction state. Card data always comes from a Snapshot."""

import curses
import time
from collections.abc import Sequence

from .events import Event, edit_events
from .markdown import Card, Column, Document, Section
from .storage import BoardFile, SectionKey, SelectionStore, Snapshot
from .view import View
from .watchers import Watchers

ENTER = ("\n", "\r", curses.KEY_ENTER)
UP = ("k", curses.KEY_UP)
DOWN = ("j", curses.KEY_DOWN)
LEFT = ("h", curses.KEY_LEFT)
RIGHT = ("l", curses.KEY_RIGHT)
PAGE = 10  # lines per PgUp/PgDn in the job view

JOB_MODES = ("jobs", "job")
# Each filter lists the job statuses it shows; None shows them all.
JOB_FILTERS: dict[str, tuple[str, ...] | None] = {
    "all": None, "failed": ("failed", "interrupted"), "running": ("running",), "queued": ("queued",),
}
JOB_REFRESH_SECONDS = 1.0
JOB_LIMIT = 500


def _clamp(index: int, size: int) -> int:
    return max(0, min(index, size - 1))


def _locate(doc: Document, event: dict) -> tuple[tuple[int, int, int], bool] | None:
    """Section, column and row of the card a watcher event names, and whether its text still matches.

    Card paths are positions, so a later move or reorder makes the recorded path
    point elsewhere. Prefer the card with the event's card ID, then one with the
    event's resulting text, nearest the recorded position, then the recorded
    position itself.
    """
    after = event.get("after") or {}
    text, card_id = after.get("text"), after.get("id")
    best = None
    for s, section in enumerate(doc.sections):
        if section.name != event.get("section"):
            continue
        for c, column in enumerate(section.columns):
            same_column = column.name == event.get("column")
            for r, (path, card) in enumerate(column.rows):
                same_id = bool(card_id) and card.id == card_id
                same_text = text is not None and text in (card.title, card.text)
                recorded = same_column and path == event.get("path")
                if not (same_id or same_text or recorded):
                    continue
                rank = (same_id, same_text, s + 1 == event.get("section_number"), same_column,
                        c + 1 == event.get("column_number"), recorded)
                if best is None or rank > best[0]:
                    best = rank, (s, c, r), same_text
    return None if best is None else (best[1], best[2])


class JobList:
    """The job view's data. Selection follows the job number, so refreshes keep it."""

    def __init__(self, watchers: Watchers):
        self.watchers = watchers
        self.all: list[dict] = []
        self.filter = "all"
        self.selected: int | None = None
        self.position = 0  # fallback when the selected job leaves the filter
        self.detail: dict | None = None
        self.error = ""
        self.loaded = float("-inf")
        self.limit = JOB_LIMIT

    @property
    def shown(self) -> list[dict]:
        statuses = JOB_FILTERS[self.filter]
        return [job for job in self.all if statuses is None or job["status"] in statuses]

    @property
    def index(self) -> int:
        shown = self.shown
        found = next((i for i, job in enumerate(shown) if job["id"] == self.selected), None)
        return _clamp(self.position if found is None else found, len(shown))

    @property
    def current(self) -> dict | None:
        shown = self.shown
        return shown[self.index] if shown else None

    def _settle(self) -> None:
        self.position = self.index
        job = self.current
        self.selected = job["id"] if job else None

    def move(self, step: int) -> None:
        self.position = self.index + step
        self.selected = None
        self._settle()

    def cycle_filter(self) -> None:
        names = list(JOB_FILTERS)
        self.filter = names[(names.index(self.filter) + 1) % len(names)]
        self._settle()

    def open(self, job_id: int) -> None:
        self.detail = self.watchers.job(job_id)

    def refresh(self, force: bool = False) -> bool:
        """Reload at most once per JOB_REFRESH_SECONDS; return whether anything changed."""
        if not force and time.monotonic() - self.loaded < JOB_REFRESH_SECONDS:
            return False
        self.loaded = time.monotonic()
        before = self.all, self.detail, self.error
        try:
            self.all = self.watchers.jobs(limit=self.limit)
            if self.detail is not None:
                self.open(self.detail["id"])
            self.error = ""
        except (OSError, RuntimeError, ValueError) as e:
            self.error = str(e)
        self._settle()
        return (self.all, self.detail, self.error) != before


class BoardUI:
    def __init__(self, source: BoardFile, snapshot: Snapshot):
        self.source = source
        self.state = SelectionStore(source.path)
        self.section: int | None = None
        self.pick = self.col = self.row = 0
        self.mode = "browse"
        self.draft = ""
        self.detail_scroll = 0
        self.show_dates = True
        self.caret = 0
        self.drag: tuple[int, int] | None = None
        self.notice = ""
        self.read_error = ""
        self.help_from = "browse"
        self.watchers = Watchers(source.path)
        self.jobs = JobList(self.watchers)
        self._install(snapshot, self.state.load())
        self.notice = next(iter(self.doc.id_warnings), "")
        try:
            self.watchers.wake()
        except (OSError, RuntimeError) as e:
            self.notice = f"Watcher recovery failed: {e}"

    @property
    def doc(self) -> Document:
        return self.snapshot.document

    @property
    def board(self) -> Section | None:
        return self.doc.sections[self.section] if self.section is not None else None

    @property
    def column(self) -> Column | None:
        board = self.board
        return board.columns[self.col] if board and board.columns else None

    @property
    def card(self) -> Card | None:
        column = self.column
        return column.rows[self.row][1] if column and column.rows else None

    def _key(self) -> SectionKey | None:
        if self.section is None:
            return None
        name = self.doc.sections[self.section].name
        occurrence = sum(s.name == name for s in self.doc.sections[: self.section])
        return name, occurrence

    def _fit(self) -> None:
        self.pick = _clamp(self.pick, len(self.doc.sections))
        board = self.board
        self.col = _clamp(self.col, len(board.columns) if board else 0)
        column = self.column
        self.row = _clamp(self.row, len(column.rows) if column else 0)

    def _install(self, snapshot: Snapshot, key: SectionKey | None) -> None:
        self.snapshot = snapshot
        self.section = None
        if key is not None:
            matches = [i for i, board in enumerate(self.doc.sections) if board.name == key[0]]
            if key[1] < len(matches):
                self.section = self.pick = matches[key[1]]
            else:
                self.state.save(None)
        self.drag = None
        if self.mode not in JOB_MODES:  # the job view does not show the board
            self.mode = "browse"
        self._fit()

    def select(self, section: int | None) -> None:
        self.section = section
        if section is not None:
            self.pick = section
        self.col = self.row = 0
        self.drag = None
        self.state.save(self._key())

    def poll(self) -> bool:
        """Return whether the view changed; cancel pending edits on an external write."""
        try:
            if not self.read_error and self.source.version() == self.snapshot.version:
                return False
            self._install(self.source.load(), self._key())
        except (OSError, UnicodeError) as e:
            changed = self.read_error != str(e)
            self.read_error = str(e)
            self.drag = None
            if self.mode not in JOB_MODES:
                self.mode = "browse"
            return changed
        self.read_error = ""
        self.notice = " · ".join(["Reloaded from disk", *self.doc.id_warnings[:1]])
        return True

    def refresh_jobs(self) -> bool:
        """Return whether the open job view changed."""
        return self.mode in JOB_MODES and self.jobs.refresh()

    def _save_edit(self, document: Document) -> bool:
        assert self.section is not None and self.column is not None
        path, _ = self.column.rows[self.row]
        events = edit_events(self.doc, document, self.section, self.col, path)
        return self._save(document, events)

    def _save(self, document: Document, events: Sequence[Event] = ()) -> bool:
        if document is self.doc:
            self.mode, self.notice = "browse", "Unchanged"
            return True
        try:
            self._install(self.source.save(document, events), self._key())
        except (OSError, UnicodeError) as e:
            self.notice = f"Not saved: {e}"
            return False
        self.notice = self.snapshot.watcher_warning or next(iter(self.doc.id_warnings), "Saved")
        return True

    def _move(self, source: int, row: int, dest: int) -> None:
        board = self.board
        if board is None or not 0 <= dest < len(board.columns) or dest == source:
            return
        _, card = board.columns[source].rows[row]
        if card.indent:
            self.notice = "Move the top-level task to move its subtree"
            return
        target = board.columns[dest]
        if self._save(self.doc.move(card, target)):
            self.col, self.row = dest, len(target.rows)
            self._fit()

    def _input(self, key: str | int) -> None:
        if key == "\x1b":
            self.mode = "browse"
        elif key in ENTER:
            column, card = self.column, self.card
            try:
                if self.mode == "add" and column:
                    if self._save(self.doc.add(column, self.draft, self.source.id_prefix(self.doc))):
                        self.row = len(column.rows)
                        self._fit()
                        added = self.card
                        if added and added.id and self.notice == "Saved":
                            self.notice = f"Added {added.id}"
                elif self.mode == "subtask" and card:
                    if self._save(self.doc.add_child(card, self.draft)) and self.column:
                        self.row = next((i for i, (_, child) in enumerate(self.column.rows)
                                         if child.start == card.end), self.row)
                elif self.mode == "owner" and card:
                    self._save_edit(self.doc.set_owner(card, self.draft or None))
                elif self.mode == "description" and card:
                    self._save(self.doc.set_description(card, self.draft))
                elif self.mode == "due" and card:
                    self._save_edit(self.doc.set_due(card, self.draft or None))
                elif card:
                    self._save_edit(self.doc.edit(card, self.draft))
            except ValueError as e:
                self.notice = str(e)
        elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
            if self.caret:
                self.draft = self.draft[: self.caret - 1] + self.draft[self.caret :]
                self.caret -= 1
        elif key == curses.KEY_DC:
            self.draft = self.draft[: self.caret] + self.draft[self.caret + 1 :]
        elif key == curses.KEY_LEFT:
            self.caret = max(0, self.caret - 1)
        elif key == curses.KEY_RIGHT:
            self.caret = min(len(self.draft), self.caret + 1)
        elif key == curses.KEY_HOME:
            self.caret = 0
        elif key == curses.KEY_END:
            self.caret = len(self.draft)
        elif self.mode == "description" and key == "\x0e":
            self.draft = self.draft[:self.caret] + "\n" + self.draft[self.caret:]
            self.caret += 1
        elif isinstance(key, str) and key.isprintable():
            self.draft = self.draft[: self.caret] + key + self.draft[self.caret :]
            self.caret += len(key)

    def _open_jobs(self) -> None:
        self.mode, self.jobs.detail = "jobs", None
        self.jobs.refresh(force=True)

    def _jump(self, job_id: int, event: dict | None) -> None:
        if self.read_error:
            self.notice = "The board file is unavailable"
            return
        found = _locate(self.doc, event or {})
        if found is None:
            self.notice = f"The card from job {job_id} is no longer on this board"
            return
        (section, col, row), same_text = found
        self.select(section)
        self.col, self.row = col, row
        self._fit()
        self.mode, self.jobs.detail = "browse", None
        self.notice = (f"Card from job {job_id}" if same_text
                       else f"Card text changed since job {job_id}; showing its recorded position")

    def _job_action(self, key: str, job: dict) -> None:
        job_id = job["id"]
        try:
            if key == "r":
                self.watchers.retry(job_id)
                self.notice = f"Queued job {job_id} for retry"
            elif key == "c":
                self.watchers.cancel(job_id)
                self.notice = f"Cancelled job {job_id}"
            else:
                self._jump(job_id, (self.jobs.detail or self.watchers.job(job_id))["event"])
                return
        except (OSError, RuntimeError, ValueError) as e:
            self.notice = str(e)
        self.jobs.refresh(force=True)

    def _jobs_key(self, key: str | int) -> None:
        jobs, detail = self.jobs, self.mode == "job"
        job = jobs.detail if detail else jobs.current
        if key == "\x1b" or (key == "w" and not detail) or (detail and key in LEFT):
            if detail:
                self.mode, jobs.detail = "jobs", None
            else:
                self.mode = "browse"
        elif key == "?":
            self.help_from, self.mode = self.mode, "help"
        elif detail and key in (*UP, *DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = PAGE if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            down = key in (*DOWN, curses.KEY_NPAGE)
            self.detail_scroll = max(0, self.detail_scroll + (step if down else -step))
        elif key in UP + DOWN:
            jobs.move(1 if key in DOWN else -1)
        elif key == "f" and not detail:
            jobs.cycle_filter()
        elif job is None:
            pass
        elif key in ENTER and not detail:
            try:
                jobs.open(job["id"])
                self.mode, self.detail_scroll = "job", 0
            except (OSError, RuntimeError, ValueError) as e:
                self.notice = str(e)
        elif key in ("r", "c", "g"):
            self._job_action(key, job)

    def key(self, key: str | int) -> bool:
        """Handle a key; False exits. Mouse events are dispatched separately."""
        self.notice = ""
        if key == curses.KEY_RESIZE:
            return True
        if self.mode in ("add", "edit", "subtask", "owner", "description", "due"):
            self._input(key)
        elif key == "q":
            return False
        elif self.mode in JOB_MODES:
            self._jobs_key(key)
        elif self.mode == "detail":
            if key in UP + DOWN:
                self.detail_scroll = max(0, self.detail_scroll + (1 if key in DOWN else -1))
            else:
                self.mode = "browse"
        elif self.mode == "help":
            self.mode, self.help_from = self.help_from, "browse"
        elif self.mode == "delete":
            if key in ("y", "Y") and self.card:
                self._save(self.doc.delete(self.card))
            self.mode = "browse"
        elif key == "?":
            self.help_from, self.mode = "browse", "help"
        elif key == "w":
            self._open_jobs()
        elif self.read_error:
            pass  # Never edit the stale snapshot while the file is unavailable.
        elif self.section is None:
            if key in UP + DOWN:
                self.pick = _clamp(self.pick + (1 if key in DOWN else -1), len(self.doc.sections))
            elif key in ENTER and self.doc.sections:
                self.select(self.pick)
        elif key in ("b", "\x1b"):
            self.select(None)
        elif key == "v":
            self.show_dates = not self.show_dates
            self.notice = "Due dates visible" if self.show_dates else "Due dates hidden"
        elif key in LEFT + RIGHT:
            self.col += 1 if key in RIGHT else -1
            self._fit()
        elif key in UP + DOWN:
            self.row += 1 if key in DOWN else -1
            self._fit()
        elif key == "a" and self.column:
            self.mode, self.draft, self.caret = "add", "", 0
        elif card := self.card:
            if key in (*ENTER, " "):
                self._save_edit(self.doc.toggle(card))
            elif key in ("H", "<", "L", ">"):
                self._move(self.col, self.row, self.col + (1 if key in ("L", ">") else -1))
            elif key in ("K", "J") and self.column:
                path = self.column.rows[self.row][0]
                changed, new_path = self.doc.reorder(self.column, path, -1 if key == "K" else 1)
                if self._save(changed) and self.column:
                    self.row = next((i for i, (p, _) in enumerate(self.column.rows)
                                     if p == new_path), self.row)
            elif key == "s":
                self.mode, self.draft, self.caret = "subtask", "", 0
            elif key == "i":
                self.mode, self.detail_scroll = "detail", 0
            elif key == "D":
                self.draft = card.description
                self.mode, self.caret = "description", len(self.draft)
            elif key in ("o", "B", "u"):
                try:
                    if key == "B":
                        self._save_edit(self.doc.set_blocked(card, not card.blocked))
                    elif key == "u":
                        self.draft = card.due
                        self.mode, self.caret = "due", len(self.draft)
                    else:
                        owner = card.field("owner")
                        self.draft = owner[0][1:] if owner else ""
                        self.mode, self.caret = "owner", len(self.draft)
                except ValueError as e:
                    self.notice = str(e)
            elif key == "e":
                # The ID is kept on save, so only the title after it is editable.
                self.mode, self.draft, self.caret = "edit", card.title, len(card.title)
            elif key == "d":
                self.mode = "delete"
        return True

    def mouse(self, view: View, x: int, y: int, buttons: int) -> None:
        if self.mode != "browse" or self.read_error:
            return
        hit = view.card_at(x, y)
        if buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            self.drag = hit
        if not buttons & (curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED):
            return
        start, self.drag = self.drag, None
        if self.section is None:
            if y in view.picks:
                self.select(view.picks[y])
        elif y == 0 and x < len("[Boards]"):
            self.select(None)
        elif (dest := view.column_at(x, y)) is not None:
            if start and start[0] != dest:
                self._move(*start, dest)
            elif hit and start == hit:
                self.col, self.row = hit
                if self.card:
                    self._save_edit(self.doc.toggle(self.card))
            else:
                self.col = dest
                self._fit()


def _apply_theme(screen: curses.window, theme: str) -> int:
    """Set the window background and return the attribute for secondary text."""
    if not curses.has_colors():
        return curses.A_DIM
    if theme == "system":
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        return curses.A_DIM
    # The 256-color cube has fixed black and white, independent of ANSI slots.
    extended = curses.COLORS >= 256
    black, white = (16, 231) if extended else (curses.COLOR_BLACK, curses.COLOR_WHITE)
    light = theme == "vercel-light"
    foreground, background = (black, white) if light else (white, black)
    try:
        curses.init_pair(1, foreground, background)
        screen.bkgd(" ", curses.color_pair(1))
        if extended:
            curses.init_pair(2, 241 if light else 247, background)
            return curses.color_pair(2)
    except curses.error:
        pass
    return curses.A_DIM


def _run(screen: curses.window, source: BoardFile, snapshot: Snapshot, theme: str) -> None:
    screen.timeout(250)
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(30)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        # Coalesce quick clicks, but get separate coordinates for a held drag.
        curses.mouseinterval(50)
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED)
    except curses.error:
        pass
    ui, view = BoardUI(source, snapshot), View(screen, _apply_theme(screen, theme))
    view.draw(ui)
    while True:
        try:
            key = screen.get_wch()
        except curses.error:  # polling timeout
            key = None
        changed = ui.poll()
        # Job selection follows the job number, so a refresh need not drop the key.
        refreshed = ui.refresh_jobs()
        # A key/click on the old layout must not act on a different card.
        if view.too_small:
            if key == "q":
                break
        elif not changed and key == curses.KEY_MOUSE:
            try:
                _, x, y, _, buttons = curses.getmouse()
            except curses.error:
                continue
            ui.mouse(view, x, y, buttons)
        elif not changed and key is not None and not ui.key(key):
            break
        if key is not None or changed or refreshed:
            view.draw(ui)


def run_tui(source: BoardFile, snapshot: Snapshot, theme: str = "system") -> None:
    try:
        curses.wrapper(_run, source, snapshot, theme)
    except curses.error as e:
        raise RuntimeError(f"terminal error: {e}; use 'kbtd list' for plain text") from e
