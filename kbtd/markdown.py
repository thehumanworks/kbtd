"""Lossless view of the parts of a Markdown document that make up a board."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from functools import cached_property
from io import StringIO


# Only a final group of standalone tokens is treated as editable metadata.
# A mention in prose, an email, and a fragment in a URL remain ordinary text.
_META = re.compile(r"[@#][\w-]+")
_WORD = re.compile(r"[^ \t]+")
_DUE_TAG = re.compile(r"#due-[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DESCRIPTION = "Description:"
# Card IDs: a leading token such as KBT-0001. Only the prefix in the board's
# counter line counts, so a title such as "CVE-2024 patch" is not an ID.
_ID = re.compile(r"([A-Z]+)-([0-9]{4,})(?=[ \t]|$)")
_COUNTER = re.compile(r"<!-- kbtd next-id: ([A-Z]+)-([0-9]+) -->")
_COUNTER_START = "<!-- kbtd next-id:"


@dataclass(frozen=True)
class Card:
    text: str
    done: bool
    start: int
    end: int  # exclusive; includes attached notes and descendants
    indent: int = 0
    children: tuple[Card, ...] = ()
    description_lines: tuple[str, ...] = ()
    prefix: str = ""  # the board's ID prefix, from its counter line

    @cached_property
    def id(self) -> str:
        m = _ID.match(self.text) if self.prefix else None
        return m[0] if m and m[1] == self.prefix else ""

    @property
    def title(self) -> str:
        """Task-line text without the card ID."""
        return self.text[len(self.id):].lstrip(" \t") if self.id else self.text

    @property
    def description(self) -> str:
        return "\n".join(self.description_lines)

    @property
    def plain_title(self) -> str:
        """Title without the card ID and the trailing group of @owner and #tag tokens."""
        end = self.metadata[0].start() if self.metadata else len(self.text)
        return self.text[len(self.id):end].strip(" \t")

    @cached_property
    def metadata(self) -> tuple[re.Match[str], ...]:
        suffix = []
        for m in reversed(list(_WORD.finditer(self.text))):
            if not _META.fullmatch(m[0]):
                break
            suffix.append(m)
        return tuple(reversed(suffix))

    @property
    def blocked(self) -> bool:
        return any(m[0] == "#blocked" for m in self.metadata)

    @cached_property
    def date_tokens(self) -> tuple[re.Match[str], ...]:
        return tuple(m for m in self.metadata if _DUE_TAG.fullmatch(m[0]))

    @cached_property
    def text_without_dates(self) -> str:
        text = self.text
        for m in reversed(self.date_tokens):
            start, end = m.span()
            text = text[:max(0, start - 1)] + text[end:]
        return text

    def field(self, kind: str) -> re.Match[str] | None:
        if kind == "due date":
            matches = self.date_tokens
        else:
            matches = [m for m in self.metadata
                       if (m[0].startswith("@") if kind == "owner" else m[0] == "#blocked")]
        if len(matches) > 1:
            raise ValueError(f"ambiguous {kind}: edit the task text to resolve duplicate tokens")
        return next(iter(matches), None)

    @property
    def due(self) -> str:
        token = self.field("due date")
        return token[0].removeprefix("#due-") if token else ""


@dataclass(frozen=True)
class Column:
    name: str
    end: int
    cards: tuple[Card, ...]

    @cached_property
    def rows(self) -> tuple[tuple[str, Card], ...]:
        """Display order; root numbers never depend on the number of descendants."""
        rows = []
        pending = [(str(n), card) for n, card in enumerate(self.cards, 1)][::-1]
        while pending:
            path, card = pending.pop()
            rows.append((path, card))
            pending.extend(reversed([(f"{path}.{n}", c) for n, c in enumerate(card.children, 1)]))
        return tuple(rows)

    def resolve(self, path: str) -> Card:
        siblings = self.cards
        for part in path.split("."):
            if not part.isdecimal() or not 1 <= int(part) <= len(siblings):
                raise ValueError(f"no task {path!r} in {self.name!r}; run 'kbtd list' for task paths")
            card = siblings[int(part) - 1]
            siblings = card.children
        return card

    @property
    def append_at(self) -> int:
        return self.cards[-1].end if self.cards else self.end


@dataclass(frozen=True)
class Section:
    name: str
    columns: tuple[Column, ...]


def _eol(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _validate_text(text: str) -> None:
    if not text.strip() or "\n" in text or "\r" in text:
        raise ValueError("card text must be a single, non-blank line")


@dataclass(frozen=True)
class Counter:
    """The board's ``<!-- kbtd next-id: KBT-0002 -->`` line."""
    line: int
    prefix: str = ""  # empty when the line is malformed
    number: int = 0


@dataclass(frozen=True)
class Document:
    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    counters: tuple[Counter, ...] = ()

    def serialize(self) -> str:
        return "".join(self.lines)

    @property
    def id_prefix(self) -> str:
        """The prefix of the board's counter line, or "" before the first ID."""
        return self.counters[0].prefix if self.counters else ""

    def _cards(self) -> Iterator[Card]:
        for section in self.sections:
            for column in section.columns:
                for _, card in column.rows:
                    yield card

    @cached_property
    def id_warnings(self) -> tuple[str, ...]:
        warnings = []
        if self.counters and not self.counters[0].prefix:
            warnings.append(f"ignoring the malformed ID counter on line {self.counters[0].line + 1}; "
                            "the next new card rewrites it")
        if len(self.counters) > 1:
            warnings.append(f"more than one ID counter line; using line {self.counters[0].line + 1}")
        seen: dict[str, int] = {}
        for card in self._cards():
            if card.id:
                seen[card.id] = seen.get(card.id, 0) + 1
        duplicates = [f"{card_id} ({n} cards)" for card_id, n in seen.items() if n > 1]
        if duplicates:
            free = f"{self.id_prefix}-{self._next_id(self.id_prefix):04d}"
            warnings.append(f"duplicate card IDs: {', '.join(duplicates)}; "
                            f"give one a free ID such as {free} with 'kbtd edit'")
        return tuple(warnings)

    def card_at(self, line: int) -> Card | None:
        """The task, at any depth, whose checkbox is on ``line``."""
        return next((card for card in self._cards() if card.start == line), None)

    def notes(self, card: Card) -> tuple[str, ...]:
        """Attached lines that are neither the description nor a subtask, without the task's indent."""
        skip = {i for child in card.children for i in range(child.start, child.end)}
        indent = " " * (card.indent + 2)
        lines = [self.lines[i].rstrip("\r\n")
                 for i in range(card.start + 1 + len(card.description_lines), card.end) if i not in skip]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return tuple(line.removeprefix(indent) for line in lines)

    def _next_id(self, prefix: str) -> int:
        """Never below the counter, nor at or below an ID already on the board.

        Reads the task text, not ``Card.id``, so IDs still count when the
        counter line is missing or malformed.
        """
        highest = 0
        for card in self._cards():
            m = _ID.match(card.text)
            if m and m[1] == prefix:
                highest = max(highest, int(m[2]))
        counter = self.counters[0] if self.counters else None
        stored = counter.number if counter and counter.prefix == prefix else 1
        return max(stored, highest + 1)

    def _updated(self, lines: list[str]) -> Document:
        # A line moved from EOF might have no terminator. Supply separators
        # where needed without normalizing existing CRLFs or the final newline.
        newline = next((e for line in self.lines if (e := _eol(line))), "\n")
        if lines:
            for i in range(len(lines) - 1):
                if not _eol(lines[i]):
                    lines[i] += newline
            if self.lines and not _eol(self.lines[-1]):
                lines[-1] = lines[-1].rstrip("\r\n")
            elif not _eol(lines[-1]):
                lines[-1] += newline
        return parse_markdown("".join(lines))

    def _described(self, line: int, description: str | None) -> Document:
        """Give the task just inserted at ``line`` its description."""
        card = self.card_at(line)
        assert card is not None
        return self.set_description(card, description) if description else self

    def add(self, column: Column, text: str, prefix: str = "", description: str | None = None) -> Document:
        """Append a card. With an ID prefix, number it and advance the counter line.

        The counter line's own prefix wins over ``prefix``, which only starts a
        board that has no valid counter yet.
        """
        _validate_text(text)
        lines = list(self.lines)
        prefix = self.id_prefix or prefix
        if not prefix:
            lines.insert(column.append_at, f"- [ ] {text}")
            return self._updated(lines)._described(column.append_at, description)
        if not re.fullmatch(r"[A-Z]+", prefix):
            raise ValueError(f"invalid card ID prefix {prefix!r}; use capital letters A-Z")
        number = self._next_id(prefix)
        given = _ID.match(text)
        if given and given[1] == prefix:  # keep an ID the caller typed
            number = max(number, int(given[2]) + 1)
        else:
            text = f"{prefix}-{number:04d} {text}"
            number += 1
        counter = f"{_COUNTER_START} {prefix}-{number:04d} -->"
        if self.counters:
            at = self.counters[0].line
            bom = "\ufeff" if at == 0 and lines[0].startswith("\ufeff") else ""
            lines[at] = bom + counter + _eol(lines[at])
        lines.insert(column.append_at, f"- [ ] {text}")
        if not self.counters:
            bom = "\ufeff" if lines and lines[0].startswith("\ufeff") else ""
            if bom:
                lines[0] = lines[0][1:]
            lines.insert(0, bom + counter)
        at = column.append_at + (0 if self.counters else 1)
        return self._updated(lines)._described(at, description)

    def add_child(self, parent: Card, text: str, description: str | None = None) -> Document:
        _validate_text(text)
        lines = list(self.lines)
        indent = parent.indent + 2
        lines.insert(parent.end, f"{' ' * indent}- [ ] {text}")
        updated = self._updated(lines)
        # Never report success if an unfinished fenced note swallowed the new line.
        added = updated.card_at(parent.end)
        if added is None or added.indent != indent:
            raise ValueError("cannot add subtask here; close the fenced note first")
        return updated._described(parent.end, description)

    def toggle(self, card: Card) -> Document:
        lines = list(self.lines)
        line = lines[card.start]
        lines[card.start] = line[:card.indent + 3] + (" " if card.done else "x") + line[card.indent + 4:]
        return self._updated(lines)

    def edit(self, card: Card, text: str) -> Document:
        """Replace the task-line text. A card keeps its ID unless ``text`` starts with one."""
        _validate_text(text)
        stripped = text.lstrip(" \t")
        given = _ID.match(stripped)
        if card.id and not (given and given[1] == card.prefix):
            text = f"{card.id} {stripped}"
        return self._text(card, text)

    def set_title(self, card: Card, title: str) -> Document:
        """Replace the title between the card ID and the trailing @owner/#tag group."""
        title = title.strip(" \t")
        if not title:
            raise ValueError("a task needs a title")
        _validate_text(title)
        end = card.metadata[0].start() if card.metadata else len(card.text)
        head = card.text[:end]
        gap = head[len(head.rstrip(" \t")):] or " "  # keep the separator before the tokens
        return self.edit(card, title + (gap + card.text[end:] if end < len(card.text) else ""))

    def _text(self, card: Card, text: str) -> Document:
        if text == card.text:
            return self
        lines = list(self.lines)
        line = lines[card.start]
        lines[card.start] = f"{line[:card.indent + 5]} {text}{_eol(line)}"
        return self._updated(lines)

    def _field(self, card: Card, kind: str, token: str) -> Document:
        m = card.field(kind)
        text = card.text
        if m:
            start, end = m.span()
            if not token:
                if start:
                    start -= 1  # remove one separator, not the surrounding whitespace
                elif text[end:].strip(" \t"):
                    end += 1
            text = text[:start] + token + text[end:]
        elif token:
            at = len(text.rstrip(" \t"))
            text = text[:at] + (" " if at else "") + token + text[at:]
        return self._text(card, text)

    def set_owner(self, card: Card, user: str | None) -> Document:
        token = ""
        if user is not None:
            token = "@" + user.removeprefix("@")
            if not _META.fullmatch(token):
                raise ValueError("owner must be one username (letters, digits, '_' or '-')")
        return self._field(card, "owner", token)

    def set_blocked(self, card: Card, blocked: bool) -> Document:
        return self._field(card, "blocked", "#blocked" if blocked else "")

    def set_due(self, card: Card, value: str | None) -> Document:
        if value is None:
            return self._field(card, "due date", "")
        token = f"#due-{value}"
        try:
            if not _DUE_TAG.fullmatch(token):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("due date must be a valid YYYY-MM-DD calendar date") from None
        return self._field(card, "due date", token)

    def set_description(self, card: Card, value: str | None) -> Document:
        value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
        values = tuple(value.split("\n")) if value else ()
        if values == card.description_lines:
            return self
        start = card.start + 1
        stop = start + len(card.description_lines)
        prefix = " " * (card.indent + 2) + _DESCRIPTION
        ending = _eol(self.lines[card.start])
        old = self.lines[start:stop]
        lines = list(self.lines)
        lines[start:stop] = [prefix + (" " + text if text else "") + (_eol(old[i]) if i < len(old) else ending)
                             for i, text in enumerate(values)]
        return self._updated(lines)

    def delete(self, card: Card) -> Document:
        lines = list(self.lines)
        del lines[card.start : card.end]
        return self._updated(lines)

    def reorder(self, column: Column, path: str, step: int) -> tuple[Document, str]:
        """Swap adjacent sibling subtrees and return the selected task's new path."""
        if step not in (-1, 1):
            raise ValueError("priority moves must be one position up or down")
        card = column.resolve(path)
        parent, separator, number = path.rpartition(".")
        siblings = column.resolve(parent).children if parent else column.cards
        target = int(number) + step
        if not 1 <= target <= len(siblings):
            return self, path

        neighbor = siblings[target - 1]
        first, last = (neighbor, card) if step < 0 else (card, neighbor)
        earlier = self.lines[first.start:first.end]
        later = list(self.lines[last.start:last.end])
        if not _eol(later[-1]):
            # Transfer the separator when moving an unterminated EOF line forward.
            later[-1] += _eol(earlier[-1])
        lines = list(self.lines)
        lines[first.start:last.end] = [*later, *lines[first.end:last.start], *earlier]
        return self._updated(lines), f"{parent}{separator}{target}"

    def move(self, card: Card, target: Column) -> Document:
        if card.indent:
            raise ValueError("move the top-level task to move its subtree")
        if card in target.cards:
            return self
        lines = list(self.lines)
        block = lines[card.start : card.end]
        at = target.append_at
        del lines[card.start : card.end]
        if at >= card.end:
            at -= len(block)
        lines[at:at] = block
        return self._updated(lines)


_HEADING = re.compile(r"(#{1,2})[ \t]+(.*)")
_TASK = re.compile(r"- \[([ x])\](?: (.*))?")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


@dataclass(frozen=True)
class _Heading:
    level: int
    name: str
    line: int


def _tree(lines: tuple[str, ...], root: Card) -> Card:
    """Index editable children within a legacy task block, keeping opaque lines intact."""
    def with_description(card: Card) -> Card:
        prefix = " " * (card.indent + 2) + _DESCRIPTION
        values = []
        for i in range(card.start + 1, root.end):
            line = lines[i].rstrip("\r\n")
            if line != prefix and not line.startswith(prefix + " "):
                break
            values.append(line[len(prefix):].removeprefix(" "))
        return replace(card, description_lines=tuple(values)) if values else card

    root = with_description(root)
    stack: list[tuple[Card, list[Card]]] = [(root, [])]

    def finish(end: int) -> None:
        node, children = stack.pop()
        stack[-1][1].append(replace(node, end=end, children=tuple(children)))

    end = root.start + 1
    fence = ""
    fence_indent = 0
    for i in range(end, root.end):
        body = lines[i].rstrip("\r\n")
        content = body.lstrip(" \t")
        if not content:
            continue
        leading = body[:len(body) - len(content)]
        indent = len(leading.expandtabs(4))
        if fence and indent >= fence_indent:
            marker = _FENCE.fullmatch(content)
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
            end = i + 1
            continue
        fence = ""
        while len(stack) > 1 and indent <= stack[-1][0].indent:
            finish(end)
        if marker := _FENCE.fullmatch(content):
            fence, fence_indent = marker[1], stack[-1][0].indent + 1
        elif (leading == " " * (stack[-1][0].indent + 2)
              and (task := _TASK.fullmatch(content))):
            child = Card(task[2] or "", task[1] == "x", i, i + 1, indent, prefix=root.prefix)
            stack.append((with_description(child), []))
        end = i + 1
    while len(stack) > 1:
        finish(end)
    return replace(root, children=tuple(stack[0][1]))


def _tokens(lines: tuple[str, ...]) -> Iterator[_Heading | Card | Counter]:
    i = 0
    fence = ""
    fence_start = 0
    while i < len(lines):
        body = lines[i].rstrip("\r\n")
        if i == 0:
            body = body.removeprefix("\ufeff")
        marker = _FENCE.fullmatch(body)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
            fence_start = i
        elif body.startswith(_COUNTER_START):
            counter = _COUNTER.fullmatch(body.rstrip(" \t"))
            yield Counter(i, counter[1], int(counter[2])) if counter else Counter(i)
        elif heading := _HEADING.fullmatch(body):
            yield _Heading(len(heading[1]), heading[2].strip(), i)
        elif task := _TASK.fullmatch(body):
            end = j = i + 1
            # Blank lines belong to the card only when followed by more notes.
            while j < len(lines):
                if lines[j].startswith((" ", "\t")):
                    end = j + 1
                elif lines[j].rstrip("\r\n"):
                    break
                j += 1
            yield Card(task[2] or "", task[1] == "x", i, end)
            i = end
            continue
        i += 1
    # Flush the final column/section. Don't append a card inside an open fence.
    yield _Heading(1, "", fence_start if fence else len(lines))


def parse_markdown(text: str) -> Document:
    # newline="" keeps physical lines and their individual terminators intact.
    lines = tuple(StringIO(text, newline=""))
    sections: list[Section] = []
    columns: list[Column] = []
    cards: list[Card] = []
    section: str | None = None
    column: str | None = None
    tokens = list(_tokens(lines))
    counters = tuple(token for token in tokens if isinstance(token, Counter))
    prefix = counters[0].prefix if counters else ""
    for token in tokens:
        if isinstance(token, Counter):
            continue
        if isinstance(token, Card):
            if column is not None:
                cards.append(_tree(lines, replace(token, prefix=prefix)))
            continue
        if column is not None:
            columns.append(Column(column, token.line, tuple(cards)))
            cards = []
            column = None
        if token.level == 1:
            if section is not None:
                sections.append(Section(section, tuple(columns)))
            section, columns = token.name, []
        elif section is not None:
            column = token.name
    return Document(lines, tuple(sections), counters)
