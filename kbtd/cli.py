"""Command-line interface shared by the console script and python -m kbtd."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import watch_cli
from .events import edit_events, move_events
from .markdown import Column, Document, Section
from .storage import BoardFile

THEMES = ("system", "vercel-light", "vercel-dark")

COMMANDS = {
    "tui": "open the interactive Kanban board (default)",
    "init": "create a starter board; never overwrite a file",
    "list": "list boards, columns and numbered cards",
    "watch": "configure background shell actions and inspect their jobs",
    "add": "append an unchecked card",
    "subtask": "append an unchecked child to the selected task",
    "show": "print the selected task and its attached Markdown",
    "owner": "set the task owner, or remove it with --clear",
    "description": "print or edit a task description, or remove it with --clear",
    "due": "print or set a calendar due date, or remove it with --clear",
    "block": "mark a task with the #blocked tag",
    "unblock": "remove the #blocked tag",
    "toggle": "toggle a card's checkbox",
    "up": "move a task and its subtree one place up among its siblings",
    "down": "move a task and its subtree one place down among its siblings",
    "move": "append a root task and its whole subtree to another column",
    "edit": "replace task-line text including inline tags/owner; keep children and notes",
    "delete": "delete this task, all its descendants and attached notes (no prompt)",
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
        if command == "watch":
            watch_cli.add_arguments(sub, common)
            continue
        if command == "tui":
            sub.add_argument("--theme", choices=THEMES, help="color theme (default: KBTD_THEME or system)")
        if command in ("tui", "init"):
            sub.add_argument("path", type=Path, nargs="?", help="Markdown file (instead of -f)")
            continue
        nargs = "?" if command == "list" else None
        sub.add_argument("section", nargs=nargs, help="section name or 1-based number")
        sub.add_argument("column", nargs=nargs, help="column name or 1-based number")
        if command not in ("list", "add"):
            sub.add_argument("card", help="1-based task path within the column, e.g. 2 or 2.1")
        if command == "move":
            sub.add_argument("target", help="destination column name or number in the same section")
        if command == "owner":
            who = sub.add_mutually_exclusive_group(required=True)
            who.add_argument("username", nargs="?", help="username with or without @")
            who.add_argument("--clear", action="store_true", help="remove the owner")
        if command in ("description", "due"):
            field = sub.add_mutually_exclusive_group()
            help_text = "description text (quote spaces)" if command == "description" else "calendar date YYYY-MM-DD"
            field.add_argument("value", nargs="?", help=help_text + "; omit to print the current value")
            field.add_argument("--clear", action="store_true", help="remove this field")
        if command in ("add", "edit", "subtask"):
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
            for path, card in lane.rows:
                print(f"     {' ' * card.indent}{path}. [{'x' if card.done else ' '}] {card.text}")


def _change(doc: Document, args: argparse.Namespace, board: Section, column: Column) -> Document:
    if args.command == "add":
        return doc.add(column, args.text)
    if args.command in ("up", "down"):
        changed, _ = doc.reorder(column, args.card, -1 if args.command == "up" else 1)
        return changed
    card = column.resolve(args.card)
    match args.command:
        case "subtask":
            return doc.add_child(card, args.text)
        case "owner":
            return doc.set_owner(card, args.username)
        case "description":
            return doc.set_description(card, args.value)
        case "due":
            return doc.set_due(card, args.value)
        case "block" | "unblock":
            return doc.set_blocked(card, args.command == "block")
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


def _task_command(source: BoardFile, doc: Document, args: argparse.Namespace) -> None:
    s = _index(doc.sections, args.section, "section")
    board = doc.sections[s]
    c = _index(board.columns, args.column, "column")
    column = board.columns[c]
    if args.command == "show":
        card = column.resolve(args.card)
        sys.stdout.write("".join(doc.lines[card.start:card.end]))
        return
    if args.command in ("description", "due") and args.value is None and not args.clear:
        card = column.resolve(args.card)
        value = card.description if args.command == "description" else card.due
        if value:
            print(value)
        return
    changed = _change(doc, args, board, column)
    if changed is doc:
        return
    events = []
    if args.command in ("toggle", "owner", "edit", "block", "unblock", "due"):
        events = edit_events(doc, changed, s, c, args.card)
    elif args.command == "move":
        events = move_events(doc, changed, s, c, args.card, _index(board.columns, args.target, "column"))
    saved = source.save(changed, events)
    if saved.watcher_warning:
        raise RuntimeError(saved.watcher_warning)


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
        if args.command == "watch":
            if args.watch_command == "add":
                source.load()
            watch_cli.manage(source.path, args)
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
            theme = getattr(args, "theme", None) or os.environ.get("KBTD_THEME") or "system"
            if theme not in THEMES:
                raise ValueError(f"invalid KBTD_THEME {theme!r}; choose from {', '.join(THEMES)}")
            run_tui(source, snapshot, theme)
        elif args.command == "list":
            _listing(snapshot.document, args.section, args.column)
        else:
            _task_command(source, snapshot.document, args)
        return 0
    except FileNotFoundError as e:
        print(f"kbtd: {e}. Use 'kbtd init' to create a new board.", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"kbtd: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 1
