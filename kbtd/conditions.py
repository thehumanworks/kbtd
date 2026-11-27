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
EVENTS = ("owner.changed", "tags.changed", "task.completed", "task.reopened")
BOOL, STR, LIST = "a boolean", "a string", "a list of strings"
MAX_LENGTH = 4096
MAX_DEPTH = 64


def _after(key: str, default: object) -> Reader:
    return lambda kinds, payload: payload.get("after", {}).get(key, default)


def _event(name: str) -> Reader:
    return lambda kinds, payload: name in kinds


# Each name's type and how to read it from (event names, event payload).
FIELDS: dict[str, tuple[str, Reader]] = {
    **{name: (BOOL, _event(name)) for name in EVENTS},
    "task.status": (STR, lambda kinds, payload: payload.get("column", "")),
    "task.section": (STR, lambda kinds, payload: payload.get("section", "")),
    "task.text": (STR, _after("text", "")),
    "task.done": (BOOL, _after("done", False)),
    "task.owners": (LIST, _after("owners", [])),
    "task.tags": (LIST, _after("tags", [])),
}
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

    def compare(self, node: ast.AST, left: ast.AST, op: ast.cmpop, right: ast.AST) -> tuple[str, Reader, bool]:
        if type(op) not in _COMPARE:
            raise self.fail(node, "only ==, !=, in and not in can compare values")
        left_kind, read_left, left_constant = self.value(left)
        right_kind, read_right, right_constant = self.value(right)
        if left_constant and right_constant:
            raise self.fail(node, "this compares two fixed values; refer to an event or a task field")
        if isinstance(op, ast.In | ast.NotIn):
            if left_kind != STR or right_kind not in (LIST, STR):
                raise self.fail(node, f"'in' needs a string on the left and a list or string on the right, "
                                      f"not {left_kind} and {right_kind}")
        elif left_kind != right_kind:
            hint = ""
            if LIST in (left_kind, right_kind) and STR in (left_kind, right_kind):
                hint = "; to test membership, write \"name\" in task.owners"
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
