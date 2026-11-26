# KBTD: task details and subtasks without a new file format

**Assignment:** Extend the Python CLI and curses TUI so a person can maintain tasks in either interface or in a text editor. All three must agree when they next read the same `TODO.md`. The file remains the only source of task data. This document defines the acceptance contract; implementation results for this checkout are recorded below.

## 1. File contract: what can an agent implement?

The authority for file syntax is [todo-md/todo-md, README at revision `d49e3c7`](https://github.com/todo-md/todo-md/blob/d49e3c762400b4fa990dcb5caba61fb1a41ea9df/README.md). Use its [Structure](https://github.com/todo-md/todo-md/blob/d49e3c762400b4fa990dcb5caba61fb1a41ea9df/README.md#structure), [Metadata](https://github.com/todo-md/todo-md/blob/d49e3c762400b4fa990dcb5caba61fb1a41ea9df/README.md#metadata) and [Example](https://github.com/todo-md/todo-md/blob/d49e3c762400b4fa990dcb5caba61fb1a41ea9df/README.md#example) sections. Use KBTD's [README](../../README.md#file-format) for the existing board behavior, not as an alternative metadata standard. The old web design in [SPEC.md](../../SPEC.md#markdown-format) is not the implementation target.

| Request | Evidence from the standard | Implementation boundary |
| --- | --- | --- |
| F1 · Description | Tasks are one-liners. The Example shows an indented URL beneath a task, but assigns it no field name or boundary. | Preserve attached text. It may be displayed as notes; do not silently convert it into an editable description or delete it when clearing a field. A distinct description needs an agreed mapping. |
| F2 · Due date | Metadata defines people and tags, not dates. | No native due date. Do not write a date marker until a convention is approved. A view toggle is a display choice, not file syntax. |
| F3 · Blocked flag and note | `#TAG` is standard; “blocked” is not a checkbox state. `- [-]` means **declined**. No blocker-note field is defined. | Implement the recommended mapping: render the exact lowercase token `#blocked` in the trailing group of space/tab-separated owner and tag tokens on the selected task line as a badge. Marking adds the token only if absent; unmarking removes just that token and one separator. Reject duplicate blocked tokens on flag edits; leave other tags, prose and checkbox state untouched. Other clients see a tag, not a new state. Treat a separately editable blocker note as unresolved even if the tag convention is approved. |
| F4 · Owner | Metadata specifies `@USERNAME`. | Ready: set, replace and remove an owner on the selected task line. Do not rewrite other tags, mentions embedded in prose, or a child's owner. If multiple owner candidates are ambiguous, report that rather than picking one for a destructive edit. |
| F5 · Subtasks | Structure and Example show a checkbox task indented two spaces under its parent. | Ready: support at least the demonstrated parent–child form. A nested checkbox is a task, not description text and not a top-level card. Do not assume arbitrary indented prose is a child. |
| F6 · Delete | Structure defines deleted as “removed from the document”. | Ready: remove the selected task, not mark it declined or done. Removing its attached subtree is KBTD's deletion policy, not an additional TODO.md state. |
| F7 · Manual editing | The examples are ordinary editable Markdown. | Document native syntax separately from any approved KBTD display conventions. Do not document a deferred field as supported. |

**Decision for non-native fields:** By default, deliver the native behaviors and report the remaining parts as deferred, not passed. If the maintainer approves conventions within standard syntax, record each exact spelling, scope, collision rule and clear operation here **before** implementation. Approval of the upstream standard alone does not approve a convention. In particular, a description and a blocker note must be distinguishable on disk before an agent promises independent edits. Do not introduce a fourth checkbox, private metadata blocks, hidden comments, IDs or a sidecar.

**Legacy files:** Upstream starts files with `# TODO` and shows tasks directly under `# BACKLOG` and `# DONE` as well as under H2 headings. Existing KBTD files use named H1 boards and H2 columns; its parser currently recognizes only open/done cards in columns. Preserve both kinds of input without a migration. In particular, retain declined tasks and tasks outside a displayed column verbatim during unrelated edits. Do not insert headings just to render a column, reinterpret declined as blocked, or claim full upstream coverage based only on KBTD-shaped examples.

## 2. Acceptance contract

For each implemented metadata field, exercise **absent → set → replace (where applicable) → clear** in both interfaces and reload from disk. “Clear” removes only the selected value. Exact command names, keys and layout are delegated; existing command syntax and top-level card numbering must keep working.

| ID | Observable pass condition |
| --- | --- |
| F1 · Description (conditional) | Both interfaces can add, inspect, edit and clear the full description. Clearing it leaves the title, children, ordinary notes and any blocker note intact. Displaying existing notes alone does not pass F1. |
| F2 · Due date (conditional) | Both interfaces can set, change and clear a calendar date. A dated card shows its date with the TUI date view on; an undated card shows none. Toggle off and on: the same date returns, with no task-file write. Reject impossible dates without writing. |
| F3 · Blocked (conditional) | The TUI distinguishes a blocked card without relying on color. Both interfaces can set and clear the flag without changing completion, column or other tags. If a note mapping is approved, both can add, read, edit and clear the obstacle/unblocking instructions. A blocked task with no note is valid; clearing the note leaves the flag set. Record flag and note results separately. |
| F4 · Owner | Assign `@alice`, replace it with `@bob`, then remove it. On each reload the interfaces agree with the file. No owner directory is required. |
| F5 · Subtasks | Add a child, edit its title and remove it. Show children beneath their parent in the TUI. With two identically named children under different parents, only the selected child changes. Moving a parent carries its subtree and attached content; sibling order survives. |
| F6 · Delete | Keep TUI confirmation and non-interactive CLI deletion. Warn when descendants will be deleted. Cancel leaves bytes unchanged; confirm removes only the selected task and its attached content. Test a leaf and a parent; preserve siblings, the next top-level task and unattached prose. |
| F7 · Manual editing | The README includes a copyable file example, how to add/change/remove each supported value by hand, and matching CLI and TUI actions. Load the hand-edited example in both interfaces as a test. |

**Proposed interaction defaults** (not file syntax): expose subtask operations in the CLI as well as the TUI; list 1-based sibling paths such as `2.1` for card 2's first child. For an approved date mapping, accept calendar input such as `2028-02-29`, show dates initially and keep visibility local to the session. If an independent blocker-note mapping is approved, unblocking clears that note, not the description.

## 3. Hand-written acceptance fixture (native syntax only)

```markdown
# TODO

## Release

- [ ] Publish guide @alice #docs
  - [ ] Review links
- [ ] Publish FAQ
  - [ ] Review links

# DONE

- [-] Retire the old guide
```

1. **Text editor → TUI:** Open this fixture with `kbtd -f TODO.md tui`. In Release, “Publish guide” has owner `alice` and child “Review links”. “Publish FAQ” has its own child with that title.
2. **TUI → disk:** Change the guide's owner to `bob`. Expected diff: only `@alice` becomes `@bob`; `#docs`, both children and the declined task remain untouched.
3. **CLI → TUI:** With no TTY, rename only the FAQ's child. Reload the TUI: the guide still has “Review links”. Remove the guide's owner; its title and tag are unchanged.
4. **TUI → disk:** Cancel deletion of “Publish guide” and compare file bytes. Confirm deletion: the guide and its child disappear, but the FAQ, its renamed child and the entire DONE section remain.

For any approved convention, add a **separate** hand-written fixture with exact before/after bytes. For the chosen `#blocked` tag mapping, marking an open `#docs` task adds only `#blocked`; unmarking restores the original line and the checkbox stays open. Do not simulate a due date or note in this fixture without settling its representation.

## 4. Delivery and verification

1. Start with the native fixture and legacy regressions. Extend shared edits in `kbtd/markdown.py`; keep `kbtd/storage.py`'s atomic save and reread. Do not write the task file on open, list, cancel or display changes.
2. Route CLI and TUI actions through the same validation. An invalid value, missing target or ambiguous edit produces an actionable error without a partial write (CLI: nonzero exit, no prompt). Keep TUI cancellation of pending input on external reload.
3. Extend `tests/test_markdown.py`, `tests/test_cli.py` and `tests/test_tui.py`. Compare edited bytes with hand-written expectations, including unrelated tags, indented prose, fences, blank lines, CRLF and final-newline state. An encoder reading its own output is not proof of compatibility.
4. Update README and help for the implemented actions. Run `python -m unittest discover -s tests -v` on Python 3.13+. Report each F-ID as passed, partial or deferred, with a test or the missing decision. Never count deferred metadata as delivered.

## 5. Results in this checkout

| ID | Result | Evidence or remaining decision |
| --- | --- | --- |
| F1 · Description | Deferred | `show` / `i` display attached Markdown as-is. A separately editable description needs an agreed representation. |
| F2 · Due date | Deferred | No approved date representation, so there is no date editor or date-view toggle. |
| F3 · Blocked | Partial | CLI `block` / `unblock` and TUI `B` implement the `#blocked` tag only. See `test_metadata_edits_touch_only_one_suffix_token` and the README board test below. A distinct blocker note remains deferred. |
| F4 · Owner | Passed | `test_task_paths_and_fields_in_headless_cli` and `test_readme_board_owner_block_hierarchy_and_delete_across_interfaces` cover setting, replacing, inspecting and clearing an owner. |
| F5 · Subtasks | Passed | Those tests cover child creation, editing and sibling paths; `test_new_actions_cancel_on_reload_and_moves_preserve_the_subtree` covers moving a parent. |
| F6 · Delete | Passed | The README board test covers TUI cancel/confirm; `test_child_ranges_preserve_parent_notes_fences_and_declined_tasks` compares the file after child and leaf deletion. |
| F7 · Manual editing | Passed for implemented features | The README board test loads the documented manual example, edits via the TUI and CLI, and compares the resulting file. |
