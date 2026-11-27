# ADR 0004 — Card IDs and the counter line

Accepted.

## Context

Card paths such as `2.1` are positions: they change when cards are added, moved or deleted. People and agents need a stable name for a card, for commit messages, watcher conditions and handovers. The requested form is `KBT-0001`: the first three letters of the GitHub repository's name, else of the folder, then four digits.

The Markdown file is the only task store (see `kbtd/storage.py`), so the ID must live in the file.

## Options

| Option | Next number | Cost |
| --- | --- | --- |
| **A comment line, `<!-- kbtd next-id: KBT-0002 -->`, at the top of the file** | Read from the line | One KBTD-owned line in the user's Markdown |
| Nothing stored; next number = highest ID in the file + 1 | Derived on every add | Deleting the newest card hands its number to the next one; renaming the repository or folder changes the prefix of new cards and restarts numbering |
| A counter in `$XDG_STATE_HOME` | Per machine | Lost on clone; two machines number independently |

## Decision

- **Where the ID lives:** as the first token of the task line, `- [ ] KBT-0001 Fix login`. A trailing `#KBT-0001` was rejected: `Card.metadata` would read it as a tag, so assigning it would emit `tags.changed`.
- **The counter line** holds the prefix and the next number. `Document.add` updates it in the same atomic write as the card. The next number is never below the line's number, nor at or below an ID already on the board, so hand-numbered cards and a deleted or malformed line are safe. The line is written by the first new card, not by `kbtd init`, so existing boards change only when they get IDs.
- **The prefix** is derived once, when the counter line is written (`kbtd/ids.py`): the repository name from a github.com `origin` remote, else the git top-level folder, else the board file's folder. Letters only, upper-cased, at most three, `TSK` if there are none. Git runs only then, with `GIT_DIR` and `GIT_WORK_TREE` removed.
- **What counts as an ID:** only a leading `PREFIX-NNNN` (four or more digits) whose prefix is the counter line's. A title such as `CVE-2024 patch` is therefore not an ID. A board without a counter line has no IDs.
- **Scope:** top-level cards added by `kbtd add` or the TUI. Subtasks get no ID and existing cards are not backfilled. Commands still address cards by path.
- **Edits keep the ID:** `Document.edit` puts the old ID back unless the new text starts with one of the board's own IDs. The TUI edits only the title.
- **Watchers:** events carry `id` and `text` without the ID in `before` and `after`, and triggers can read `task.id`. The TUI's go-to-card prefers the job's card ID over its text.

## Consequences

- Two branches that each add cards produce duplicate IDs after a merge, since copies of a file share no counter. `kbtd list`, `kbtd add` and the TUI report duplicates; the user renumbers one with `kbtd edit`.
- Changing the prefix in the counter line stops older IDs from counting as IDs. They stay in the text.
- Addressing cards by ID in commands, a `task.created` event and subtask IDs are left for later.
