"""Lossless view of the parts of a Markdown document that make up a board."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO


@dataclass(frozen=True)
class Card:
    text: str
    done: bool
    start: int
    end: int  # exclusive; includes attached, indented notes


@dataclass(frozen=True)
class Column:
    name: str
    end: int
    cards: tuple[Card, ...]

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
class Document:
    lines: tuple[str, ...]
    sections: tuple[Section, ...]

    def serialize(self) -> str:
        return "".join(self.lines)

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

    def add(self, column: Column, text: str) -> Document:
        _validate_text(text)
        lines = list(self.lines)
        lines.insert(column.append_at, f"- [ ] {text}")
        return self._updated(lines)

    def toggle(self, card: Card) -> Document:
        lines = list(self.lines)
        line = lines[card.start]
        lines[card.start] = line[:3] + (" " if card.done else "x") + line[4:]
        return self._updated(lines)

    def edit(self, card: Card, text: str) -> Document:
        _validate_text(text)
        lines = list(self.lines)
        lines[card.start] = f"{lines[card.start][:5]} {text}{_eol(lines[card.start])}"
        return self._updated(lines)

    def delete(self, card: Card) -> Document:
        lines = list(self.lines)
        del lines[card.start : card.end]
        return self._updated(lines)

    def move(self, card: Card, target: Column) -> Document:
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


def _tokens(lines: tuple[str, ...]) -> Iterator[_Heading | Card]:
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
    for token in _tokens(lines):
        if isinstance(token, Card):
            if column is not None:
                cards.append(token)
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
    return Document(lines, tuple(sections))
