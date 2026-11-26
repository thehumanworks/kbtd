"""Curses event loop and interaction state. Card data always comes from a Snapshot."""

import curses

from .markdown import Card, Column, Document, Section
from .storage import BoardFile, SectionKey, SelectionStore, Snapshot
from .view import View

ENTER = ("\n", "\r", curses.KEY_ENTER)
UP = ("k", curses.KEY_UP)
DOWN = ("j", curses.KEY_DOWN)
LEFT = ("h", curses.KEY_LEFT)
RIGHT = ("l", curses.KEY_RIGHT)


def _clamp(index: int, size: int) -> int:
    return max(0, min(index, size - 1))


class BoardUI:
    def __init__(self, source: BoardFile, snapshot: Snapshot):
        self.source = source
        self.state = SelectionStore(source.path)
        self.section: int | None = None
        self.pick = self.col = self.row = 0
        self.mode = "browse"
        self.draft = ""
        self.detail_scroll = 0
        self.caret = 0
        self.drag: tuple[int, int] | None = None
        self.notice = ""
        self.read_error = ""
        self._install(snapshot, self.state.load())

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
            self.mode, self.drag = "browse", None
            return changed
        self.read_error = ""
        self.notice = "Reloaded from disk"
        return True

    def _save(self, document: Document) -> bool:
        if document is self.doc:
            self.mode, self.notice = "browse", "Unchanged"
            return True
        try:
            self._install(self.source.save(document), self._key())
        except (OSError, UnicodeError) as e:
            self.notice = f"Not saved: {e}"
            return False
        self.notice = "Saved"
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
                    if self._save(self.doc.add(column, self.draft)):
                        self.row = len(column.rows)
                        self._fit()
                elif self.mode == "subtask" and card:
                    if self._save(self.doc.add_child(card, self.draft)) and self.column:
                        self.row = next((i for i, (_, child) in enumerate(self.column.rows)
                                         if child.start == card.end), self.row)
                elif self.mode == "owner" and card:
                    self._save(self.doc.set_owner(card, self.draft or None))
                elif card:
                    self._save(self.doc.edit(card, self.draft))
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
        elif isinstance(key, str) and key.isprintable():
            self.draft = self.draft[: self.caret] + key + self.draft[self.caret :]
            self.caret += len(key)

    def key(self, key: str | int) -> bool:
        """Handle a key; False exits. Mouse events are dispatched separately."""
        self.notice = ""
        if key == curses.KEY_RESIZE:
            return True
        if self.mode in ("add", "edit", "subtask", "owner"):
            self._input(key)
        elif key == "q":
            return False
        elif self.mode == "detail":
            if key in UP + DOWN:
                self.detail_scroll = max(0, self.detail_scroll + (1 if key in DOWN else -1))
            else:
                self.mode = "browse"
        elif self.mode == "help":
            self.mode = "browse"
        elif self.mode == "delete":
            if key in ("y", "Y") and self.card:
                self._save(self.doc.delete(self.card))
            self.mode = "browse"
        elif key == "?":
            self.mode = "help"
        elif self.read_error:
            pass  # Never edit the stale snapshot while the file is unavailable.
        elif self.section is None:
            if key in UP + DOWN:
                self.pick = _clamp(self.pick + (1 if key in DOWN else -1), len(self.doc.sections))
            elif key in ENTER and self.doc.sections:
                self.select(self.pick)
        elif key in ("b", "\x1b"):
            self.select(None)
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
                self._save(self.doc.toggle(card))
            elif key in ("H", "<", "L", ">"):
                self._move(self.col, self.row, self.col + (1 if key in ("L", ">") else -1))
            elif key == "s":
                self.mode, self.draft, self.caret = "subtask", "", 0
            elif key == "i":
                self.mode, self.detail_scroll = "detail", 0
            elif key in ("o", "B"):
                try:
                    if key == "B":
                        self._save(self.doc.set_blocked(card, not card.blocked))
                    else:
                        owner = card.field("owner")
                        self.draft = owner[0][1:] if owner else ""
                        self.mode, self.caret = "owner", len(self.draft)
                except ValueError as e:
                    self.notice = str(e)
            elif key == "e":
                self.mode, self.draft, self.caret = "edit", card.text, len(card.text)
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
                    self._save(self.doc.toggle(self.card))
            else:
                self.col = dest
                self._fit()


def _run(screen: curses.window, source: BoardFile, snapshot: Snapshot) -> None:
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
    accent = curses.A_BOLD
    if curses.has_colors():
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            accent |= curses.color_pair(1)
        except curses.error:
            pass
    ui, view = BoardUI(source, snapshot), View(screen, accent)
    view.draw(ui)
    while True:
        try:
            key = screen.get_wch()
        except curses.error:  # polling timeout
            key = None
        changed = ui.poll()
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
        if key is not None or changed:
            view.draw(ui)


def run_tui(source: BoardFile, snapshot: Snapshot) -> None:
    try:
        curses.wrapper(_run, source, snapshot)
    except curses.error as e:
        raise RuntimeError(f"terminal error: {e}; use 'kbtd list' for plain text") from e
