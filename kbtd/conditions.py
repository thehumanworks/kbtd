"""Watcher triggers: Python expressions about one saved task edit.

Python's own parser (``ast``) reads a trigger. This module then compiles only
an allow-listed subset of the syntax tree into a matcher, checking types as it
goes. Nothing the user writes is executed, and mistakes such as comparing a
list with a string are reported when the watcher is registered, not silently
evaluated as False on every edit. See docs/adr/0003-python-triggers.md.
"""

import ast
import re
from collections.abc import Callable
from functools import lru_cache

type Matcher = Callable[[tuple[str, ...], dict], bool]
type Reader = Callable[[tuple[str, ...], dict], object]

# Event names double as boolean names: true when this edit emitted the event.
EVENTS = ("owner.changed", "tags.changed", "task.completed", "task.reopened", "task.status.changed",
          "task.description.changed")
BOOL, STR, LIST = "a boolean", "a string", "a list of strings"
# The old or new value when this edit changed it, otherwise None:
# task.status.changed_from/_to and task.description.changed_from/_to.
CHANGE = "a string only when this edit changed it (None otherwise)"
MAX_LENGTH = 4096
MAX_DEPTH = 64


def _after(key: str, default: object) -> Reader:
    return lambda kinds, payload: payload.get("after", {}).get(key, default)


def _event(name: str) -> Reader:
    return lambda kinds, payload: name in kinds


def _changed(key: str, side: str) -> Reader:
    """``before`` or ``after`` value of ``key`` when the edit emitted ``task.KEY.changed``."""
    event = f"task.{key}.changed"

    def read(kinds: tuple[str, ...], payload: dict) -> object:
        if event not in kinds:
            return None
        snapshot = payload.get(side)
        value = snapshot.get(key) if isinstance(snapshot, dict) else None
        if value is None and key == "status" and side == "after":
            value = payload.get("column")  # the payload names the new column too
        return value if isinstance(value, str) else None
    return read


def _owner_set(snapshot: object) -> set[str] | None:
    match snapshot:
        case {"owners": list() as owners} if all(isinstance(owner, str) for owner in owners):
            return set(owners)
    return None


def _owner_difference(source: str, other: str) -> Reader:
    def read(kinds: tuple[str, ...], payload: dict) -> list[str]:
        if "owner.changed" not in kinds:
            return []
        owners = _owner_set(payload.get(source))
        others = _owner_set(payload.get(other))
        # A missing snapshot cannot establish who joined or left.
        if owners is None or others is None:
            return []
        return sorted(owners - others)

    return read


# Each name's type and how to read it from (event names, event payload).
FIELDS: dict[str, tuple[str, Reader]] = {
    **{name: (BOOL, _event(name)) for name in EVENTS},
    "owner.added": (LIST, _owner_difference("after", "before")),
    "owner.removed": (LIST, _owner_difference("before", "after")),
    "task.id": (STR, _after("id", "")),
    "task.status": (STR, lambda kinds, payload: payload.get("column", "")),
    "task.section": (STR, lambda kinds, payload: payload.get("section", "")),
    "task.text": (STR, _after("text", "")),
    "task.description": (STR, _after("description", "")),
    "task.done": (BOOL, _after("done", False)),
    "task.owners": (LIST, _after("owners", [])),
    "task.tags": (LIST, _after("tags", [])),
    "task.status.changed_from": (CHANGE, _changed("status", "before")),
    "task.status.changed_to": (CHANGE, _changed("status", "after")),
    "task.description.changed_from": (CHANGE, _changed("description", "before")),
    "task.description.changed_to": (CHANGE, _changed("description", "after")),
}
# What `kbtd watch triggers` and `kbtd watch --help` say about each name.
# tests/test_cli.py checks that every event and field has an entry.
HELP = {
    "owner.changed": "an owner is assigned, replaced or cleared",
    "tags.changed": "trailing tags change, incl. #blocked and #due-YYYY-MM-DD",
    "task.completed": "checkbox goes from open to done",
    "task.reopened": "checkbox goes from done to open",
    "task.status.changed": "the task moves to another column",
    "task.description.changed": "the description is added, edited or removed",
    "owner.added": "owners assigned by this edit, sorted, without @",
    "owner.removed": "owners cleared by this edit, sorted, without @",
    "task.id": 'card ID, e.g. "KBT-0001"; "" if none',
    "task.status": "column name",
    "task.section": "board (H1) name",
    "task.text": "task-line text, without the card ID",
    "task.description": 'description, lines joined by "\\n"; "" if none',
    "task.done": "checkbox",
    "task.owners": "owners, sorted, without @",
    "task.tags": "tags, sorted, without #",
    "task.status.changed_from": "old column on a move, else None",
    "task.status.changed_to": "new column on a move, else None",
    "task.description.changed_from": "old description when it changed, else None",
    "task.description.changed_to": "new description when it changed, else None",
}
# Short type names for the reference's TYPE column.
_KIND = {BOOL: "boolean", STR: "string", LIST: "list of strings", CHANGE: "string or None"}


def reference() -> str:
    """Every event, field and operator a trigger can use, as plain text."""
    width = max(map(len, FIELDS))
    events = [f"  {name:<{width}} {HELP[name]}" for name in EVENTS]
    fields = [f"  {name:<{width}} {_KIND[FIELDS[name][0]]:<16} {HELP[name]}"
              for name in FIELDS if name not in EVENTS]
    return "\n".join([
        "EVENTS     true when the edit emitted it", *events, "",
        "FIELDS     value after the edit", *fields, "",
        "OPERATORS  and  or  not  ==  !=  in  not in  ( )",
        'VALUES     "quoted strings", True, False, ["lists", "of", "strings"]',
        'CHANGED    *.changed_from/_to allow only NAME == "x", "x" in NAME and NAME in [...]',
        "NO EVENT   creating, reordering or deleting a card",
        "",
        "Example:",
        "  kbtd watch add codex-backlog 'owner.changed and \"codex\" in task.owners and "
        "task.status == \"Backlog\"' 'CMD'",
    ])


_COMPARE: dict[type, Callable[[object, object], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_LEGACY = ("/\\", "\\/", "~")


class _Invalid(ValueError):
    pass


class _Compiler:
    def __init__(self, source: str):
        self.source = source

    def text(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or type(node).__name__

    def fail(self, node: ast.AST, message: str) -> _Invalid:
        return _Invalid(f"{message} (in {self.text(node)!r})")

    def condition(self, node: ast.AST) -> Matcher:
        """Something that is true or false: an event, a comparison, a list field."""
        kind, read, constant = self.value(node)
        if constant:
            raise self.fail(node, "a fixed value is not a condition; refer to an event or a task field")
        if kind == BOOL:
            return read  # type: ignore[return-value]
        if kind == CHANGE:
            name = self.text(node)
            raise self.fail(node, f"{name} is a string or None, not a condition; "
                                  f"use {_change_event(name)} to test for the change")
        if kind == LIST:
            return lambda kinds, payload: bool(read(kinds, payload))  # true when not empty
        raise self.fail(node, f"{self.text(node)} is {kind}, not a condition; compare it with == or in")

    def value(self, node: ast.AST) -> tuple[str, Reader, bool]:
        """Return (type, reader, is_constant) for one node of the allowed subset."""
        match node:
            case ast.BoolOp(op=op, values=values):
                parts = [self.condition(value) for value in values]
                if isinstance(op, ast.And):
                    return BOOL, lambda kinds, payload: all(part(kinds, payload) for part in parts), False
                return BOOL, lambda kinds, payload: any(part(kinds, payload) for part in parts), False
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                inner = self.condition(operand)
                return BOOL, lambda kinds, payload: not inner(kinds, payload), False
            case ast.UnaryOp():
                raise self.fail(node, "use 'not' to negate a condition")
            case ast.Compare(left=left, ops=[op], comparators=[right]):
                return self.compare(node, left, op, right)
            case ast.Compare():
                raise self.fail(node, "combine comparisons with 'and' instead of chaining them")
            case ast.Attribute() | ast.Name():
                return self.field(node)
            case ast.Constant(value=bool() | str() as constant):
                kind = BOOL if isinstance(constant, bool) else STR
                return kind, lambda kinds, payload: constant, True
            case ast.List(elts=items) | ast.Tuple(elts=items):
                if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in items):
                    raise self.fail(node, "a list in a trigger may only contain quoted strings")
                values = [item.value for item in items]  # type: ignore[attr-defined]
                return LIST, lambda kinds, payload: values, True
            case ast.Constant():
                raise self.fail(node, "only quoted strings, True and False are allowed as values")
        raise self.fail(node, f"{type(node).__name__} expressions are not allowed in a watcher trigger")

    def field(self, node: ast.AST) -> tuple[str, Reader, bool]:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            raise self.fail(node, "only event names and task fields can be read")
        name = ".".join([node.id, *reversed(parts)])
        if name in FIELDS:
            kind, read = FIELDS[name]
            return kind, read, False
        if name in ("TRUE", "FALSE", "true", "false"):
            raise _Invalid(f"unknown name {name!r}; write True or False")
        import difflib  # only needed to explain a mistake

        close = difflib.get_close_matches(name, FIELDS, n=1)
        hint = f"; did you mean {close[0]!r}?" if close else f"; use one of {', '.join(FIELDS)}"
        raise _Invalid(f"unknown name {name!r}{hint}")

    def compare_change(self, node: ast.AST, op: ast.cmpop, left: tuple[str, Reader, ast.AST],
                       right: tuple[str, Reader, ast.AST]) -> tuple[str, Reader, bool]:
        name = self.text(left[2] if left[0] == CHANGE else right[2])
        event = _change_event(name)
        # Without the change these names are None, and None != "x" is true. So !=
        # and "not in" would match every other edit; refuse them.
        if isinstance(op, ast.NotEq | ast.NotIn):
            raise self.fail(node, f"{name} is None unless {event}, so != and 'not in' would also match "
                                  f"every other edit; write {event} and not {name} == \"...\"")
        kinds = left[0], right[0]
        if (isinstance(op, ast.Eq) and set(kinds) == {CHANGE, STR}
                or isinstance(op, ast.In) and kinds in ((CHANGE, LIST), (STR, CHANGE))):
            return _change_test(op, left[1], right[1])
        raise self.fail(node, f'compare {name} with == "text", "text" in {name} or {name} in ["a", "b"]')

    def compare(self, node: ast.AST, left: ast.AST, op: ast.cmpop, right: ast.AST) -> tuple[str, Reader, bool]:
        if type(op) not in _COMPARE:
            raise self.fail(node, "only ==, !=, in and not in can compare values")
        left_kind, read_left, left_constant = self.value(left)
        right_kind, read_right, right_constant = self.value(right)
        if left_constant and right_constant:
            raise self.fail(node, "this compares two fixed values; refer to an event or a task field")
        if CHANGE in (left_kind, right_kind):
            return self.compare_change(node, op, (left_kind, read_left, left), (right_kind, read_right, right))
        if isinstance(op, ast.In | ast.NotIn):
            if left_kind != STR or right_kind not in (LIST, STR):
                raise self.fail(node, f"'in' needs a string on the left and a list or string on the right, "
                                      f"not {left_kind} and {right_kind}")
        elif left_kind != right_kind:
            hint = ""
            if LIST in (left_kind, right_kind) and STR in (left_kind, right_kind):
                member, collection = (right, left) if left_kind == LIST else (left, right)
                hint = f"; to test membership, write {self.text(member)} in {self.text(collection)}"
            raise self.fail(node, f"cannot compare {left_kind} with {right_kind}{hint}")
        test = _COMPARE[type(op)]
        return BOOL, lambda kinds, payload: test(read_left(kinds, payload), read_right(kinds, payload)), False


def _syntax_hint(source: str) -> str:
    # The most likely causes: the operators of the earlier TLA+-style syntax, or
    # a line break outside parentheses.
    if any(symbol in source for symbol in _LEGACY):
        return "; triggers are Python expressions: use and, or, not instead of /\\, \\/, ~"
    if re.search(r"(?<![=!<>])=(?!=)", source):
        return '; compare with ==, and test owners or tags with "name" in task.owners'
    if "\n" in source.strip():
        return "; wrap a trigger that spans lines in parentheses"
    return ""


def _change_event(name: str) -> str:
    """task.status.changed_to -> task.status.changed"""
    return name.rpartition(".")[0] + ".changed"


def _change_test(op: ast.cmpop, left: Reader, right: Reader) -> tuple[str, Reader, bool]:
    test = _COMPARE[type(op)]

    def read(kinds: tuple[str, ...], payload: dict) -> bool:
        a, b = left(kinds, payload), right(kinds, payload)
        return a is not None and b is not None and test(a, b)  # None: the value did not change
    return BOOL, read, False


def _depth(node: ast.AST) -> int:
    deepest, stack = 0, [(node, 1)]
    while stack:
        current, depth = stack.pop()
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(current))
    return deepest


@lru_cache(maxsize=256)
def compile_trigger(source: str) -> Matcher:
    """Compile a trigger into ``matcher(event_names, payload) -> bool``, or raise ValueError."""
    if not source.strip():
        raise ValueError("a watcher trigger needs an event name or a condition")
    if len(source) > MAX_LENGTH:
        raise ValueError(f"a watcher trigger may have at most {MAX_LENGTH} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        where = f"line {exc.lineno} column {exc.offset}" if exc.lineno and exc.lineno > 1 else f"column {exc.offset}"
        raise ValueError(f"invalid watcher trigger: {exc.msg} at {where}{_syntax_hint(source)}") from None
    except (RecursionError, MemoryError):
        raise ValueError("a watcher trigger is nested too deeply") from None
    if _depth(tree.body) > MAX_DEPTH:
        raise ValueError("a watcher trigger is nested too deeply")
    compiler = _Compiler(source.strip())
    try:
        return compiler.condition(tree.body)
    except _Invalid as exc:
        raise ValueError(f"invalid watcher trigger: {exc}") from None
