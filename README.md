# KBTD — Markdown Kanban in the terminal

KBTD is a Python CLI and curses TUI for Markdown task boards using [TODO.md](https://github.com/todo-md/todo-md) task syntax. Both interfaces edit the Markdown file itself. No database, browser or server is involved.

## Quick start

### Run with mise

With [mise](https://mise.jdx.dev/) on your PATH (Linux, macOS or WSL), run these in the directory where you want your board:

```sh
mise exec github:thehumanworks/kbtd -- kbtd init
mise exec github:thehumanworks/kbtd -- kbtd        # open ./TODO.md in the TUI
```

The `github:` backend needs a [published release](https://github.com/thehumanworks/kbtd/releases) with the `kbtd.tar.gz` asset; it does not run the repository's main branch. The command after `--` is required. The launcher uses mise to install the Python build in the release's `mise.lock` on first use. No system Python, Nix or devenv is needed.

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

Or run `uv sync` and use `uv run kbtd` in place of `python -m kbtd`. Installing the project (`pip install .`) also provides the `kbtd` command. The CLI needs only the standard library. The TUI requires an interactive terminal and Python with curses (standard on most Unix builds; on Windows use WSL or install `windows-curses`).

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
  Keep links relative.
  - [ ] Review links
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
| Mark or unmark as blocked | Add or remove the ordinary tag `#blocked`, e.g. `- [ ] Publish guide @alice #docs #blocked`. KBTD displays **BLOCKED**; other TODO.md clients see a tag. The checkbox is unchanged. |
| Add, edit or delete a subtask | Add a checkbox line indented **two more spaces** than its parent. Edit its text in place. To delete, remove its line and attached subtree, not adjacent siblings. |

Put editable metadata together at the end of the task line, separated by spaces or tabs. Token names use letters, digits, underscores and hyphens. KBTD does not edit mentions inside prose or URLs. The blocked tag is case-sensitive. If the trailing group contains multiple owners or duplicate blocked tags, resolve that field by editing the line rather than having KBTD choose for you.

Other indented text is ordinary Markdown. It travels with its task and can be read with `i` or `kbtd show`. No typed description, due date or separate blocker-note syntax is defined. Preambles, fenced examples and unrelated prose stay in the file. Edit headings by hand to create or rename boards and columns.

## CLI

The default file is `./TODO.md`. Use `-f PATH` / `--file PATH` to use a different file. `kbtd PATH` opens that file in the TUI.

```text
kbtd init [PATH]                      Create Tasks with Backlog, In Progress, Done
kbtd tui [PATH]                       Open the section picker or last selected board
kbtd list [SECTION [COLUMN]]          List numbered sections, columns and cards
kbtd add SECTION COLUMN TEXT          Append an unchecked card
kbtd toggle SECTION COLUMN CARD       Toggle completion
kbtd move SECTION COLUMN CARD TARGET  Append card to TARGET in the same section
kbtd edit SECTION COLUMN CARD TEXT    Replace task-line text, including any inline tokens
kbtd subtask SECTION COLUMN CARD TEXT Append a child to CARD
kbtd owner SECTION COLUMN CARD USER   Set or replace owner (or use --clear instead of USER)
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

| Key / gesture | Action |
| --- | --- |
| ↑/↓ or `k`/`j` | Select a board in the picker, or a card in a column |
| Enter | Open the selected board, or toggle the selected card |
| ←/→ or `h`/`l` | Focus a column |
| Space / click card | Toggle completion |
| `H`/`L` or `<`/`>` | Move a root task and its subtree to the previous/next column |
| Drag card to another column | Append the root and its subtree to that column (mouse-capable terminals) |
| `a` / `e` | Add a card / edit its text; Enter saves, Esc cancels |
| `s` | Add a child to the selected task; Enter saves, Esc cancels |
| `o` | Edit the owner; save an empty value to clear |
| `B` | Mark or unmark the selected task as blocked |
| `i` | Inspect the selected Markdown subtree, including notes; ↑/↓ scroll, Esc returns |
| `d` | Delete selected task, its descendants and notes after confirmation |
| `b` / Esc / click `[Boards]` | Return to the section picker |
| `?` / `q` | Show shortcuts / quit |

Edits replace the file atomically, then re-read it. The TUI checks for external changes every 250 ms. Changes reload the board and cancel pending input; if its H1 disappears, you return to the picker. There is no merge or locking: concurrent saves are last-write-wins. Only the selected section is remembered between sessions, keyed by the absolute Markdown path under `$XDG_STATE_HOME/kbtd` (default `~/.local/state/kbtd`).

## Publishing a mise release

`mise run package` builds `dist/kbtd.tar.gz` with the launcher, Python sources, `mise.toml` and `mise.lock`. Push a version tag such as `v0.1.0` to have [the release workflow](.github/workflows/release.yml) test and publish that archive. To run a specific published version, use e.g. `mise exec github:thehumanworks/kbtd@0.1.0 -- kbtd list`.

To refresh the locked Python patch version, run `mise lock --bump python` and commit the updated `mise.lock`.

## Tests

```sh
python -m unittest discover -s tests -v
```
