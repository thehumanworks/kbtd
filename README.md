# KBTD — Markdown Kanban in the terminal

KBTD is a Python CLI and curses TUI for Markdown task boards using [TODO.md](https://github.com/todo-md/todo-md) task syntax. Both interfaces edit the Markdown file itself. Optional watchers run your shell commands after board edits, as durable [DBOS](https://docs.dbos.dev/) workflows stored in a local SQLite file.

## Quick start

### Run with mise

With [mise](https://mise.jdx.dev/) on your PATH (Linux, macOS or WSL), run these in the directory where you want your board:

```sh
mise exec github:thehumanworks/kbtd -- kbtd init
mise exec github:thehumanworks/kbtd -- kbtd        # open ./TODO.md in the TUI
```

The `github:` backend needs a [published release](https://github.com/thehumanworks/kbtd/releases) with the `kbtd.tar.gz` asset; it does not run the repository's main branch. The command after `--` is required. On first use, the launcher installs the Python and uv builds pinned in the release's `mise.lock`. uv then installs the Python dependencies pinned in `uv.lock` into the install's `.venv`. No system Python, Nix or devenv is needed.

From a checkout (also works before the first release):

```sh
mise trust                 # after reviewing mise.toml
mise install --locked
mise exec -- kbtd --help
mise run test
```

### Run with Python

With Python 3.13+ in this checkout:

```sh
python -m kbtd init                     # create ./TODO.md (never overwrites)
python -m kbtd add Tasks Backlog "Write the RFC"
python -m kbtd                          # choose a board and press Enter
```

Or run `uv sync` and use `uv run kbtd` in place of `python -m kbtd`. Installing the project (`pip install .`) also provides the `kbtd` command. Board commands and the TUI need only the standard library. Watchers need the `dbos` package, which `uv sync` and `pip install .` install. The TUI requires an interactive terminal and Python with curses (standard on most Unix builds; on Windows use WSL or install `windows-curses`).

### Run with Nix or devenv

From this checkout, with Nix flakes enabled, no Python setup is needed:

```sh
nix run . -- --help                  # default package is kbtd
nix run .#kbtd -- path/to/TODO.md    # open an existing board
nix build .#kbtd                     # produces ./result/bin/kbtd
```

With devenv, run `devenv shell -- kbtd --help` (or just `kbtd` inside the shell).
`devenv build outputs.kbtd` builds the standalone package without entering a shell.
Both use `package.nix`; the dev shell keeps its Python environment for development
without needing an editable install of the CLI.

## File format

Copy this board into `TODO.md` and open it with `kbtd`:

```markdown
# TODO

## Release

- [ ] Publish guide @alice #docs
  Description: Explain how to publish a release.
  Keep links relative.
  - [ ] Review links #due-2028-02-29
- [ ] Publish FAQ
  - [ ] Review links

## Ready

# DONE

- [-] Retire the old guide
```

Each H1 is a selectable board and each H2 under it a column. KBTD edits open (`- [ ]`) and done (`- [x]`) tasks in columns. Existing named boards still work. Tasks directly under an H1 and declined tasks (`- [-]`) are preserved, not displayed as cards. Declined does **not** mean blocked.

| To edit by hand | What to write in `TODO.md` |
| --- | --- |
| Assign, change or remove an owner | Add, replace or remove one trailing `@USERNAME`, as in `Publish guide @alice #docs`. |
| Describe a task or subtask | Put `Description: text` directly below its checkbox, indented two spaces further. Repeat the prefix for each additional line. A bare `Description:` encodes a blank line. Delete the leading description lines to clear the field. |
| Set or remove a due date | Add, replace or remove a trailing `#due-YYYY-MM-DD` tag. KBTD displays the date on the card. |
| Mark or unmark as blocked | Add or remove the ordinary tag `#blocked`, e.g. `- [ ] Publish guide @alice #docs #blocked`. KBTD displays **BLOCKED**; other TODO.md clients see a tag. The checkbox is unchanged. |
| Add, edit or delete a subtask | Add a checkbox line indented **two more spaces** than its parent. Edit its text in place. To delete, remove its line and attached subtree, not adjacent siblings. |

Put editable metadata together at the end of the task line, separated by spaces or tabs. Token names use letters, digits, underscores and hyphens. KBTD does not edit mentions inside prose or URLs. The blocked tag is case-sensitive. If the trailing group contains multiple owners or duplicate blocked tags, resolve that field by editing the line rather than having KBTD choose for you.

The description prefix and due-date tag are KBTD conventions. Other indented text travels with its task and can be read with `i` or `kbtd show`. Preambles, fenced examples and unrelated prose stay in the file. Edit headings by hand to create or rename boards and columns.

## CLI

The default file is `./TODO.md`. Use `-f PATH` / `--file PATH` to use a different file. `kbtd PATH` opens that file in the TUI.

```text
kbtd init [PATH]                      Create Tasks with Backlog, In Progress, Done
kbtd tui [--theme THEME] [PATH]       Open the section picker or last selected board
kbtd list [SECTION [COLUMN]]          List numbered sections, columns and cards
kbtd add SECTION COLUMN TEXT          Append an unchecked card
kbtd toggle SECTION COLUMN CARD       Toggle completion
kbtd move SECTION COLUMN CARD TARGET  Append card to TARGET in the same section
kbtd up SECTION COLUMN CARD           Raise a task or subtask one place among siblings
kbtd down SECTION COLUMN CARD         Lower a task or subtask one place among siblings
kbtd edit SECTION COLUMN CARD TEXT    Replace task-line text, including any inline tokens
kbtd subtask SECTION COLUMN CARD TEXT Append a child to CARD
kbtd owner SECTION COLUMN CARD USER   Set or replace owner (or use --clear instead of USER)
kbtd description SECTION COLUMN CARD [TEXT | --clear]  Read, set or clear a description
kbtd due SECTION COLUMN CARD [YYYY-MM-DD | --clear]    Read, set or clear a calendar date
kbtd block SECTION COLUMN CARD        Add the #blocked tag
kbtd unblock SECTION COLUMN CARD      Remove the #blocked tag
kbtd show SECTION COLUMN CARD         Print the task and its attached Markdown
kbtd delete SECTION COLUMN CARD       Delete the whole subtree and notes, without prompting
```

Section and column selectors are exact names or **1-based numbers** in file order. Numeric selectors always mean positions. For duplicate names, use the numbers from `list`. Card paths are 1-based positions local to the column, **not** persistent IDs: `2` is the second root, `2.1` its first child. Children do not change root numbers. Use `list` again after adding, moving or deleting tasks. Quote names and text that contain spaces.

```sh
# For the Release column in the example above:
kbtd owner TODO Release 1 bob
kbtd block TODO Release 1
kbtd subtask TODO Release 1 "Check screenshots"
kbtd edit TODO Release 2.1 "Review FAQ links"
kbtd show TODO Release 1
kbtd unblock TODO Release 1
kbtd owner TODO Release 1 --clear
kbtd delete TODO Release 1.2
```

## TUI

The board shows columns side by side, with cards stacked inside them. A narrow terminal shows a window of columns that follows the selection. Completed cards have an `[x]` and muted text.

The default `system` theme follows your terminal’s foreground and background. Use `--theme` for one session, or set `KBTD_THEME` in your shell profile to choose a default for all boards. The flag takes precedence.

```sh
kbtd tui --theme vercel-light       # dark text on white
export KBTD_THEME=vercel-dark       # white text on black
kbtd                                # use the shell preference
kbtd tui --theme system             # use terminal colors for this session
```

Both Vercel themes use gray for secondary text on 256-color terminals. Basic color terminals use their black and white palette. Monochrome terminals retain selection and completion cues through reverse video and checkboxes.

| Key / gesture | Action |
| --- | --- |
| ↑/↓ or `k`/`j` | Select a board in the picker, or a card in a column |
| Enter | Open the selected board, or toggle the selected card |
| ←/→ or `h`/`l` | Focus a column |
| Space / click card | Toggle completion |
| `H`/`L` or `<`/`>` | Move a root task and its subtree to the previous/next column |
| `K` / `J` | Raise / lower the selected task or subtask one place among its siblings. Its notes, descendants and selection move with it. |
| Drag card to another column | Append the root and its subtree to that column (mouse-capable terminals) |
| `a` / `e` | Add a card / edit its text; Enter saves, Esc cancels |
| `s` | Add a child to the selected task; Enter saves, Esc cancels |
| `o` | Edit the owner; save an empty value to clear |
| `D` | Edit the description. Ctrl-N inserts a line break, Enter saves, Esc cancels. An empty draft clears it. |
| `u` / `v` | Edit the due date / toggle date visibility for this session. Enter saves the date. An empty draft clears it. |
| `B` | Mark or unmark the selected task as blocked |
| `i` | Inspect the selected Markdown subtree, including notes; ↑/↓ scroll, Esc returns |
| `w` | Open this file's [watcher jobs](#job-history-and-recovery), from the picker or a board |
| `d` | Delete selected task, its descendants and notes after confirmation |
| `b` / Esc / click `[Boards]` | Return to the section picker |
| `?` / `q` | Show shortcuts / quit |

Edits replace the file atomically, then re-read it. The TUI checks for external changes every 250 ms. Changes reload the board and cancel pending input; if its H1 disappears, you return to the picker. There is no merge or locking: concurrent saves are last-write-wins. The selected section is remembered between sessions, keyed by the absolute Markdown path under `$XDG_STATE_HOME/kbtd` (default `~/.local/state/kbtd`).

## Watchers

A watcher runs a shell command in the board file’s directory when a KBTD edit triggers it. Definitions apply to every section of that file and belong to the user who registered them. Register a command once from the CLI. Edits from the CLI and TUI can both trigger it.

This example records assignments in a local log on a fresh board.

```sh
kbtd init
kbtd add Tasks Backlog "Publish the guide"
kbtd watch add log-owners owner.changed 'printf "%s\n" "$KBTD_EVENT" >> assignments.jsonl'
kbtd owner Tasks Backlog 1 alice
kbtd watch jobs
```

`watch jobs` shows the job ID, watcher, trigger, status, attempt and result. Once the job succeeds, `assignments.jsonl` contains a JSON event with the task path and the owners before and after the edit. The board command returns at once. A background worker starts the action within about a second.

| Trigger | When it fires on an existing task or subtask |
| --- | --- |
| `owner.changed` | An owner is assigned, replaced or cleared. |
| `tags.changed` | The set of trailing tags changes. This includes `#blocked` and due-date tags. |
| `task.completed` | The checkbox changes from open to done. |
| `task.reopened` | The checkbox changes from done to open. |
| `task.status.changed` | The task moves to another column: `kbtd move`, `H`/`L`, `<`/`>` or a drag in the TUI. Its subtasks move with it but emit nothing. |

To log assignments to Codex in Backlog, pass the whole condition as one shell argument.

```sh
kbtd watch add codex-backlog '"codex" in owner.added and task.status == "Backlog"' 'printf "%s\n" "$KBTD_EVENT" >> codex.jsonl'
```

A condition is a Python expression. KBTD reads it with Python's own parser and evaluates only the names and operators below. It never runs the text as code.

| Name | Type | Value |
| --- | --- | --- |
| `owner.changed`, `tags.changed`, `task.completed`, `task.reopened`, `task.status.changed` | boolean | Whether this edit emitted that event |
| `owner.added`, `owner.removed` | list of strings | Owners who joined or left in this edit, sorted and unique, without `@` |
| `task.status` | string | Column name after the edit |
| `task.status.changed_from`, `task.status.changed_to` | string or `None` | Column before / after a move. `None` when the edit did not move the task. |
| `task.section` | string | Board (H1) name |
| `task.text` | string | Task-line text after the edit |
| `task.done` | boolean | Checkbox after the edit |
| `task.owners`, `task.tags` | list of strings | Owners and tags after the edit, sorted, without `@` and `#` |

For a handoff from Sam to Codex, use `"sam" in owner.removed and "codex" in owner.added`. Adding Sam while Codex stays assigned matches `owner.changed and "codex" in task.owners`. Only adding Codex matches `"codex" in owner.added`.

Combine conditions with `and`, `or`, `not` and parentheses. Compare with `==`, `!=`, `in` and `not in`. Values are quoted strings, `True`, `False`, or lists of strings such as `["Backlog", "Ready"]`. A list name on its own is true when the list is not empty, and `"bug" in task.text` tests for a substring. Comparisons are case-sensitive. Wrap a condition that spans several lines in parentheses.

`kbtd watch add` rejects a condition that cannot do what it says: an unknown name (`task.owner`), a comparison of different types (`task.owners == "codex"`), or a fixed value used as a condition (`owner.changed and "codex"`). The error names the part to fix.

`task.status.changed_to == "Done"` matches only the move into Done, and `task.status.changed_from == "Backlog"` only a move out of Backlog. `task.status == "Done"` matches any event-emitting edit of a task that is now in Done. Because the move names are `None` without a move, `!=` and `not in` on them would also match every other edit, so KBTD refuses them. For "moved, but not into Done", write `task.status.changed and not task.status.changed_to == "Done"`.

For example, `(owner.changed or tags.changed) and task.status == "In Progress"` runs once for an edit in that column, even if the edit changes both fields. Each edit is evaluated independently. Conditions run only on edits that emit at least one event in the table above.

Earlier versions used `/\`, `\/`, `~` and `=`. A watcher saved with that syntax no longer runs. `kbtd watch list` names it, and edits report that it was skipped. Remove it and register it again in the new syntax.

For conditional watchers, `KBTD_TRIGGER` holds the registered expression and `KBTD_EVENT.events` in the JSON payload lists the event names from the edit.

Raw title edits can trigger owner and tag changes. Creating or reordering a card, moving it within its column, saving an unchanged value, and reloading an external edit produce no events. For a move, the event's column and path give the task's new place, and `before.status` its old column. Watcher actions that call KBTD inherit a job ID that suppresses nested dispatch.

Use `kbtd watch list` to see definitions and `kbtd watch remove NAME` to stop future matches.

### Job history and recovery

| Status | Meaning |
| --- | --- |
| `queued` | The edit recorded the job. It waits for the worker. |
| `running` | The action started. After a reboot or a killed worker, the job shows `running` until the next worker reruns it. |
| `succeeded` / `failed` | The command exited with code 0 / another code, or could not start. |
| `interrupted` | The worker stopped during each of three starts. KBTD stops rerunning it. |
| `cancelled` | Someone cancelled the job before it started, with `kbtd watch cancel`, the TUI or the DBOS tools. |

Actions run **at least once**. If the worker stops during an action, the next worker runs it again, up to three starts in total. Opening the TUI, listing jobs or an edit that matches a watcher starts that worker. `KBTD_ATTEMPT` tells the command which start this is. An agent launcher can use `KBTD_BOARD` and `KBTD_JOB_ID` together as an idempotency key to create or return the same agent run on every attempt. A later removal and re-assignment generates a new assignment job with its own key.

`kbtd watch show ID` prints one job as JSON: its command, event, exit code and the last 8 KiB of the command's combined output. `kbtd watch retry ID` reruns a failed, interrupted or cancelled job with its original command and event. A retry can repeat a shell side effect. `kbtd watch cancel ID` takes a queued job off the queue. It cannot stop a command that has started, and a job the worker picks up at that moment may still run. Right after an edit, a job may take about a second to reach the worker; until then, cancel asks you to try again.

In the TUI, `w` opens the same history, newest first, and refreshes it every second. It shows the newest 500 jobs.

| Key | Job list | Job details |
| --- | --- | --- |
| ↑/↓ | Select a job | Scroll; PgUp/PgDn scroll a page |
| Enter | Show the command, event, exit code and output tail | |
| `r` / `c` | Retry / cancel the selected job | Retry / cancel this job |
| `g` | Go to the card that triggered the job | Same |
| `f` | Show all, failed or interrupted, running, or queued jobs | |
| Esc | Back to the board or picker | Back to the list |

Card paths are positions, so `g` first looks in the job's section for a card with the text the edit left, nearest its recorded column and path. If none matches, it selects the recorded position and says that the text changed.

Each board's watchers live in one SQLite file under `$XDG_STATE_HOME/kbtd/watchers` (default `~/.local/state/kbtd/watchers`). It is a standard DBOS system database, so the [DBOS CLI](https://docs.dbos.dev/python/reference/cli) can inspect it too, for example `uv run dbos workflow steps 3 -s sqlite:///PATH`. Worker diagnostics go to the `.log` file next to it. [ADR 0001](docs/adr/0001-board-watchers.md) defines triggers and the event contract. [ADR 0002](docs/adr/0002-dbos-watcher-runtime.md) explains the DBOS runtime and how it can move to Postgres.

## Publishing a mise release

`mise run package` builds `dist/kbtd.tar.gz` with the launcher, Python sources, `mise.toml`, `mise.lock`, `pyproject.toml` and `uv.lock`. Push a version tag such as `v0.1.0` to have [the release workflow](.github/workflows/release.yml) test and publish that archive. To run a specific published version, use e.g. `mise exec github:thehumanworks/kbtd@0.1.0 -- kbtd list`.

To refresh the locked Python patch version, run `mise lock --bump python` and commit the updated `mise.lock`.

## Tests

```sh
uv run python -m unittest discover -s tests -v   # or: mise run test
```

The watcher tests start real DBOS workers, so the suite takes about 25 seconds.
