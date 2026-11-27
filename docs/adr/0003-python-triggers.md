# ADR 0003 — Watcher triggers are Python expressions

Accepted. This record replaces the trigger syntax of [ADR 0001](0001-board-watchers.md). Events, payloads and the rule "one job per matching watcher per edit" do not change.

## Context

Conditional triggers used a TLA+-style syntax (`owner.changed = "codex" /\ task.status = "Backlog"`). A hand-written tokenizer and a shunting-yard parser in `kbtd/conditions.py` read it. The concern: a parsing bug would make watchers silently fail to react.

**Evidence.** A differential fuzz compared the old parser with a reference evaluator on 50,000 random expressions, with and without full parentheses and with irregular spacing. There were no mismatches, so no current bug was found. The risk is in maintenance and use:

- Every grammar change means editing a custom tokenizer and precedence table.
- The operators `/\` and `\/` contain backslashes. JSON decodes `"\/"` to `/`, and other quoting layers can change them too.
- `owner.changed = "codex"` silently means two things: the event happened, and the resulting owners contain `codex`.

## Options

| Option | Parsing | Cost |
| --- | --- | --- |
| Keep the TLA+ syntax and use a parser library (Lark, pyparsing) | A library runs a grammar that we still write and test | A dependency in every edit, and the backslash and quoting problems remain |
| **Python expressions, read with the standard library's `ast`** | CPython's own parser | A syntax change. kbtd has no release yet, so few saved watchers are affected |

## Decision

A trigger is a Python expression. `ast.parse(source, mode="eval")` produces the syntax tree. `kbtd/conditions.py` compiles only an allow-listed subset into a matcher, with a type for every node:

- **Names:** the event names (booleans: did this edit emit the event), plus `task.id` (added by [ADR 0004](0004-card-ids.md)), `task.status`, `task.section`, `task.text` (strings), `task.done` (boolean), and `task.owners` and `task.tags` (lists of strings), all read after the edit.
- **Owner transitions:** `owner.added` and `owner.removed` are sorted, unique lists of case-sensitive owner names without `@`. On an `owner.changed` event they give the set differences after minus before and before minus after. Both lists are empty if the event is absent or either snapshot lacks a list of strings for `owners`. An explicit empty list is valid.
- **Operators:** `and`, `or`, `not`, parentheses, and single comparisons with `==`, `!=`, `in`, `not in`.
- **Values:** quoted strings, `True`, `False`, and lists of quoted strings.

Nothing else compiles. Calls, attribute access on values, subscripts, arithmetic, comprehensions and f-strings are all rejected, and the source is never passed to `eval`. That matters once triggers can come from a shared database. Registration also rejects expressions that would compile in Python but not do what they say:

- A fixed value used as a condition (`owner.changed and "codex"`), or a comparison of two fixed values.
- A comparison of different types (`task.owners == "codex"`), with a hint to use `in`.
- An unknown name, with the closest known name suggested.
- More than 4096 characters, or more than 64 nesting levels. This bounds the work an edit can trigger.

A list name on its own is true when the list is not empty. `in` on a string tests for a substring, as in Python.

The two lists use existing membership and truthiness rules. They support both a named assignment (`"codex" in owner.added`) and any removal (`owner.removed`). Exposing the previous task would require authors to combine two membership tests for each named transition. Dedicated predicate functions would require adding calls to the compiler.

## Amendment: status changes (2026-11-27)

Moving a task between columns used to emit no event, so `task.status == "Done"` matched only edits made while a task was already in Done, never the move into it. A move now emits `task.status.changed`, with three names:

| Name | Value |
| --- | --- |
| `task.status.changed` | Whether this edit moved the task to another column |
| `task.status.changed_from` | The column before the move, or `None` |
| `task.status.changed_to` | The column after the move, or `None` |

Only `==` and `in` compare the two column names. `None != "Done"` is true, so `!=` and `not in` would match every edit that is not a move. Registration refuses them and suggests `task.status.changed and not task.status.changed_to == "Done"`. Only the moved task emits the event: its subtasks change column with it, but a parent move would otherwise produce one job per descendant. The event payload names the task's new column and path, and every snapshot gains `status`. Moves inside one column remain reorders and emit nothing.

## Migration

Watchers saved with the old syntax no longer compile. Dispatch now compiles each watcher on its own. An invalid one is skipped, the other watchers still react, and the edit reports `Board saved. Skipped watcher 'name': invalid trigger`. `kbtd watch list` prints the reason on stderr. The old syntax is not translated automatically, because a translator would need the parser this change removes.

| Old | New |
| --- | --- |
| `owner.changed = "codex"` | `owner.changed and "codex" in task.owners` |
| `tags.changed = "docs"` | `tags.changed and "docs" in task.tags` |
| `task.status = "Backlog"` | `task.status == "Backlog"` |
| `task.completed = TRUE` | `task.completed` |
| `a /\ b`, `a \/ b`, `~a` | `a and b`, `a or b`, `not a` |

The owner translation above also matches edits to other owners while Codex stays assigned. Choose `"codex" in owner.added` to run only when Codex joins. Each match queues one job. Recovery and explicit retries run its recorded command and event under the same job ID ([ADR 0002](0002-dbos-watcher-runtime.md)).

## Verification

The test suite compares the compiler with a reference evaluator on 3,000 random conditions per run, and lists the rejected mistakes with their messages. A one-off run of 100,000 random conditions found no mismatch.
