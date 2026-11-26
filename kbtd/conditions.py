"""Value predicates and Boolean expressions for one saved task edit."""

import json
import re
from collections.abc import Callable, Iterator
from functools import lru_cache

EVENT_FIELDS = {
    "owner.changed": "owners",
    "tags.changed": "tags",
    "task.completed": "done",
    "task.reopened": "done",
}
type Matcher = Callable[[tuple[str, ...], dict], bool]

# The fallback token ensures that invalid punctuation is reported, never skipped.
_TOKENS = re.compile(r'\s+|/\\|\\/|"(?:[^"\\]|\\.)*"|[\w.]+|.', re.DOTALL)
_AND = "/\\"
_OR = "\\/"
_PRECEDENCE = {"(": 0, _OR: 1, _AND: 2, "~": 3}


def _predicate(name: str, value: str | bool | None) -> Matcher:
    if name == "task.status":
        return lambda kinds, payload: payload.get("column") == value
    if value is None:
        return lambda kinds, payload: name in kinds

    field = EVENT_FIELDS[name]
    if field == "done":
        return lambda kinds, payload: name in kinds and payload.get("after", {}).get(field) is value
    return lambda kinds, payload: name in kinds and value in payload.get("after", {}).get(field, ())


class _Parser:
    def __init__(self, source: str):
        self.tokens: Iterator[str] = (m[0] for m in _TOKENS.finditer(source) if not m[0].isspace())
        self.token = next(self.tokens, "")

    def take(self) -> str:
        current = self.token
        self.token = next(self.tokens, "")
        return current

    def accept(self, token: str) -> bool:
        if self.token != token:
            return False
        self.take()
        return True

    def parse(self) -> list[Matcher | str]:
        steps: list[Matcher | str] = []
        operators: list[str] = []
        need_term = True
        while self.token:
            if need_term:
                if self.token in ("~", "("):
                    operators.append(self.take())
                    continue
                steps.append(self.predicate())
                need_term = False
            elif self.accept(")"):
                while operators and operators[-1] != "(":
                    steps.append(operators.pop())
                if not operators:
                    raise ValueError("unexpected ')' in watcher trigger")
                operators.pop()
            elif self.token in (_AND, _OR):
                operator = self.take()
                while operators and _PRECEDENCE[operators[-1]] >= _PRECEDENCE[operator]:
                    steps.append(operators.pop())
                operators.append(operator)
                need_term = True
            else:
                raise ValueError(f"unexpected token in watcher trigger: {self.token!r}")
        if need_term:
            raise ValueError("expected a watcher trigger at end of expression")
        for operator in reversed(operators):
            if operator == "(":
                raise ValueError("expected ')' in watcher trigger")
            steps.append(operator)
        return steps

    def predicate(self) -> Matcher:
        name = self.take()
        if name not in EVENT_FIELDS and name != "task.status":
            raise ValueError(f"unknown watcher trigger: {name!r}")
        if not self.accept("="):
            if name == "task.status":
                raise ValueError('task.status requires a column name, e.g. task.status = "Backlog"')
            return _predicate(name, None)

        literal = self.take()
        if EVENT_FIELDS.get(name) == "done":
            if literal not in ("TRUE", "FALSE"):
                raise ValueError(f"{name} expects TRUE or FALSE")
            return _predicate(name, literal == "TRUE")
        if not literal.startswith('"'):
            raise ValueError(f"{name} expects a double-quoted string")
        try:
            value = json.loads(literal)
        except ValueError as exc:
            raise ValueError(f"invalid quoted string for {name}: {exc}") from exc
        return _predicate(name, value)


@lru_cache(maxsize=256)
def compile_trigger(source: str) -> Matcher:
    steps = _Parser(source).parse()
    if len(steps) == 1 and callable(steps[0]):
        return steps[0]

    def matches(kinds: tuple[str, ...], payload: dict) -> bool:
        # Evaluate in postfix order so nested conditions do not consume the
        # caller's stack during a CLI or TUI save.
        stack: list[bool] = []
        for step in steps:
            if callable(step):
                stack.append(step(kinds, payload))
            elif step == "~":
                stack[-1] = not stack[-1]
            else:
                right = stack.pop()
                if step == _AND:
                    stack[-1] = stack[-1] and right
                else:
                    stack[-1] = stack[-1] or right
        return stack[0]

    return matches
