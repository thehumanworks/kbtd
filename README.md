# KBTD — Markdown Kanban in the terminal

KBTD is a Python CLI and curses TUI for the TODO file format in [SPEC.md](SPEC.md). Both interfaces edit the Markdown file itself. No database, browser or server is involved.

## Quick start

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

```markdown
# Project Alpha

## Backlog

- [ ] Write the RFC
  Keep the example short.

## In Progress

- [x] Design mockups

# Project Beta

## Ideas

- [ ] Try a new layout
```

Each H1 is a selectable board. Its H2s are columns; top-level `- [ ]` and `- [x]` items are cards. The checkbox state is independent of the column. Indented lines belong to the preceding card: they are **not** shown as cards but travel with it when moved (and are removed if it is deleted). Preambles, prose, blank lines and other non-board content stay in the file. Examples inside fenced code blocks are not boards or cards. Create or rename boards and columns by editing their headings in Markdown.

## CLI

The default file is `./TODO.md`. Use `-f PATH` / `--file PATH` to use a different file. `kbtd PATH` opens that file in the TUI.

```text
kbtd init [PATH]                      Create Tasks with Backlog, In Progress, Done
kbtd tui [PATH]                       Open the section picker or last selected board
kbtd list [SECTION [COLUMN]]          List numbered sections, columns and cards
kbtd add SECTION COLUMN TEXT          Append an unchecked card
kbtd toggle SECTION COLUMN CARD       Toggle completion
kbtd move SECTION COLUMN CARD TARGET  Append card to TARGET in the same section
kbtd edit SECTION COLUMN CARD TEXT    Replace only the card text
kbtd delete SECTION COLUMN CARD       Delete the card, including any nested notes
```

Section and column selectors are exact names or **1-based numbers** in file order. Numeric selectors always mean positions. For duplicate names, use the numbers from `list`. Card numbers are local to their column, **not** persistent IDs. Quote names and text that contain spaces.

```sh
kbtd -f notes.md list
kbtd -f notes.md move 'Project Alpha' Backlog 1 'In Progress'
kbtd -f notes.md toggle 1 2 1
```

## TUI

The board shows columns side by side, with cards stacked inside them. A narrow terminal shows a window of columns that follows the selection. Completed cards have an `[x]` and muted text.

| Key / gesture | Action |
| --- | --- |
| ↑/↓ or `k`/`j` | Select a board in the picker, or a card in a column |
| Enter | Open the selected board, or toggle the selected card |
| ←/→ or `h`/`l` | Focus a column |
| Space / click card | Toggle completion |
| `H`/`L` or `<`/`>` | Move card to the previous/next column |
| Drag card to another column | Append it to that column (mouse-capable terminals) |
| `a` / `e` | Add a card / edit its text; Enter saves, Esc cancels |
| `d` | Delete selected card and notes after confirmation |
| `b` / Esc / click `[Boards]` | Return to the section picker |
| `?` / `q` | Show shortcuts / quit |

Edits replace the file atomically, then re-read it. The TUI checks for external changes every 250 ms. Changes reload the board and cancel pending input; if its H1 disappears, you return to the picker. There is no merge or locking: concurrent saves are last-write-wins. Only the selected section is remembered between sessions, keyed by the absolute Markdown path under `$XDG_STATE_HOME/kbtd` (default `~/.local/state/kbtd`).

## Tests

```sh
python -m unittest discover -s tests -v
```
