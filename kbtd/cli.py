"""Command-line interface shared by the console script and python -m kbtd."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .markdown import Column, Document, Section
from .storage import BoardFile

COMMANDS = {
    "tui": "open the interactive Kanban board (default)",
    "init": "create a starter board; never overwrite a file",
    "list": "list boards, columns and numbered cards",
    "add": "append an unchecked card",
    "toggle": "toggle a card's checkbox",
    "move": "append a card and its notes to another column",
    "edit": "change a card's text, keeping its notes",
    "delete": "remove a card and its indented notes",
}


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-f", "--file", type=Path, default=argparse.SUPPRESS,
                        metavar="PATH", help="Markdown file (default: ./TODO.md)")
    parser = argparse.ArgumentParser(
        prog="kbtd", parents=[common], description="Manage a Markdown TODO file as a Kanban board.",
        epilog="kbtd PATH is shorthand for kbtd tui PATH. Use 'kbtd COMMAND --help' for arguments.",
    )
    subs = parser.add_subparsers(dest="command", metavar="COMMAND")
    for command, help_text in COMMANDS.items():
        sub = subs.add_parser(command, help=help_text, description=help_text, parents=[common])
        if command in ("tui", "init"):
            sub.add_argument("path", type=Path, nargs="?", help="Markdown file (instead of -f)")
            continue
        nargs = "?" if command == "list" else None
        sub.add_argument("section", nargs=nargs, help="section name or 1-based number")
        sub.add_argument("column", nargs=nargs, help="column name or 1-based number")
        if command not in ("list", "add"):
            sub.add_argument("card", type=int, help="1-based card number in the source column")
        if command == "move":
            sub.add_argument("target", help="destination column name or number in the same section")
        if command in ("add", "edit"):
            sub.add_argument("text", help="one line of task text (quote spaces)")
    return parser


def _index(items: Sequence[Section | Column], selector: str, kind: str) -> int:
    if selector.isdecimal():
        n = int(selector)
        if 1 <= n <= len(items):
            return n - 1
        raise ValueError(f"no {kind} numbered {n}; run 'kbtd list' to see the numbers")
    matches = [i for i, item in enumerate(items) if item.name == selector]
    if len(matches) > 1:
        raise ValueError(f"ambiguous {kind} {selector!r}; use its number from 'kbtd list'")
    if not matches:
        raise ValueError(f"no {kind} named {selector!r}")
    return matches[0]


def _listing(doc: Document, section: str | None, column: str | None) -> None:
    if not doc.sections and section is None:
        print("No boards. Add a '# Section' and a '## Column' to the Markdown file.")
    boards = range(len(doc.sections)) if section is None else [_index(doc.sections, section, "section")]
    for s in boards:
        board = doc.sections[s]
        print(f"# {s + 1}. {board.name}")
        lanes = range(len(board.columns)) if column is None else [_index(board.columns, column, "column")]
        for c in lanes:
            lane = board.columns[c]
            print(f"  ## {c + 1}. {lane.name}")
            for n, card in enumerate(lane.cards, 1):
                print(f"     {n}. [{'x' if card.done else ' '}] {card.text}")


def _change(doc: Document, args: argparse.Namespace) -> Document:
    board = doc.sections[_index(doc.sections, args.section, "section")]
    column = board.columns[_index(board.columns, args.column, "column")]
    if args.command == "add":
        return doc.add(column, args.text)
    if not 1 <= args.card <= len(column.cards):
        raise ValueError(f"no card numbered {args.card} in {column.name!r}")
    card = column.cards[args.card - 1]
    match args.command:
        case "edit":
            return doc.edit(card, args.text)
        case "delete":
            return doc.delete(card)
        case "toggle":
            return doc.toggle(card)
        case "move":
            return doc.move(card, board.columns[_index(board.columns, args.target, "column")])
        case _:
            raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 1 and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv.insert(0, "tui")
    parser = _parser()
    args = parser.parse_args(argv)
    path = getattr(args, "path", None)
    if path is not None and hasattr(args, "file"):
        parser.error("choose either PATH or --file, not both")
    try:
        source = BoardFile(path or getattr(args, "file", Path("TODO.md")))
        if args.command == "init":
            source.create()
            print(f"Created {source.path}")
            return 0
        snapshot = source.load()
        if args.command in (None, "tui"):
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("the TUI needs an interactive terminal; use 'kbtd list' for plain text")
            try:
                from .tui import run_tui
            except ModuleNotFoundError as e:
                if e.name not in ("curses", "_curses"):
                    raise
                raise RuntimeError("the TUI needs Python curses support (on Windows, install windows-curses)") from e
            run_tui(source, snapshot)
        elif args.command == "list":
            _listing(snapshot.document, args.section, args.column)
        else:
            changed = _change(snapshot.document, args)
            if changed is not snapshot.document:
                source.save(changed)
        return 0
    except FileNotFoundError as e:
        print(f"kbtd: {e}. Use 'kbtd init' to create a new board.", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"kbtd: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 1
