---
name: kbtd-watchers
description: How kbtd's board watchers run on DBOS. Covers the outbox in kbtd/watchers.py, the detached worker and workflow in kbtd/watch_worker.py, job statuses, recovery, the tests, and how to inspect a board's job database. Use this skill whenever you change, debug or explain anything about `kbtd watch` commands, watcher triggers that enqueue jobs, KBTD_* action environment variables, job history or retries, the watcher SQLite file under $XDG_STATE_HOME/kbtd/watchers, or the plan to move watchers to Postgres or the cloud. Pair it with the dbos-python skill for DBOS API details.
---

# kbtd watchers on DBOS

Read `docs/adr/0002-dbos-watcher-runtime.md` first for the reasons. This skill covers how to change the code without breaking its guarantees.

## Flow of one job

```
CLI/TUI save ──► storage.BoardFile.save ──► Watchers.emit_many ──► kbtd_outbox row (sqlite3, one transaction)
                                                       │
                                                       └─► Watchers.wake ─► python -I worker (detached, logs to <key>.log)
worker: take <key>.lock ─► DBOS.launch (recovers PENDING) ─► _hand_off: outbox row ─► DBOS workflow id=<job number>
        ─► queue kbtd-watchers (worker_concurrency=1, priority=<job number>) ─► watcher_action ─► run_command step
        ─► idle twice ─► DBOS.destroy ─► release lock ─► wake again if work arrived meanwhile
```

One SQLite file per board holds the DBOS tables plus `kbtd_watchers` and `kbtd_outbox`. Its path is `Watchers(board).path`, and `Watchers(board).url` gives the DBOS URL.

## Invariants to keep

1. **The edit path never imports DBOS.** `emit_many`, `wake` and `_has_work` use `sqlite3` only, because `import dbos` costs about 0.45 s on every CLI edit. `tests/test_watchers.py::test_board_edits_record_jobs_without_importing_dbos` checks this. Code that needs DBOS (`add`, `jobs`, `job`, `retry`, `cancel`, the worker) imports it inside the function. The TUI's job view (`w`, `JobList` in `kbtd/tui.py`) calls `jobs`, so DBOS loads only when that view opens.
2. **One live worker per board.** The `.lock` file (an SQLite write lock that the OS releases when the process dies) is what makes DBOS startup recovery safe. Never launch DBOS on a board's database without holding it, except through `DBOSClient`, which does not recover anything.
3. **Job number = workflow ID = priority.** `kbtd_outbox` uses `AUTOINCREMENT`, so numbers are never reused. Reuse would make a handoff "return-existing" and silently drop the new job. The priority keeps strict job order inside one millisecond.
4. **Portable JSON only.** The workflow uses `WorkflowSerializationFormat.PORTABLE`. Keep inputs, outputs and attributes to JSON types. Errors return as `PortableWorkflowError`: read `.message`.
5. **One step per side effect.** `watcher_action` has exactly one step, `kbtd.run_command`. Count real starts inside that step (the `attempt` attribute), not with `recovery_attempts`.
6. **Version pinning.** `APP_VERSION` in `kbtd/watchers.py` must change when the steps of `watcher_action` change: renaming, adding, removing or reordering steps, or changing step inputs. A worker runs only its own version or unversioned jobs, so old `PENDING` jobs need a migration note.
7. **Action contract.** ADR 0001 plus ADR 0002 define it. The command runs in the board's directory with `KBTD_BOARD`, `KBTD_TRIGGER`, `KBTD_JOB_ID`, `KBTD_EVENT` and `KBTD_ATTEMPT`. stdin is closed. stdout and stderr go to a temporary file whose last 8 KiB is kept. Nested kbtd edits are suppressed because `KBTD_JOB_ID` is set.

## Status mapping (used by `watch jobs` and `watch show`)

`queued` = outbox row, `ENQUEUED` or `DELAYED` · `running` = `PENDING` · `succeeded` = `SUCCESS` · `failed` = `ERROR` · `interrupted` = `MAX_RECOVERY_ATTEMPTS_EXCEEDED` · `cancelled` = `CANCELLED`. `retry` rewinds `failed`, `interrupted` and `cancelled` jobs with `rewind_workflow(id, start_step=1)`. `cancel` calls `cancel_workflow` only for `ENQUEUED` or `DELAYED` jobs. It refuses outbox rows, because deleting one would race `_hand_off`.

## Common tasks

**Run the tests** (the watcher tests start real workers; the suite takes about 25 s):

```sh
uv run python -m unittest discover -s tests -v      # or: mise run test
uv run python -m unittest tests.test_watchers -v    # watcher tests only
```

**Inspect a board's jobs:**

```sh
kbtd watch jobs -f path/to/TODO.md
kbtd watch show 3 -f path/to/TODO.md                 # command, event, exit code, output tail
DB=$(uv run python -c 'import sys; from pathlib import Path; from kbtd.watchers import Watchers; print(Watchers(Path(sys.argv[1])).url)' path/to/TODO.md)
uv run dbos workflow list -s "$DB" -d -l 5
uv run dbos workflow steps 3 -s "$DB"
```

Worker problems (import errors, DBOS errors) appear in the `.log` file next to the database. Expected action failures (`ActionFailed`) are filtered out of it.

**Add an action feature** (for example a timeout or retries for failing commands):

- A retry or timeout of the *command* belongs in `run_command` or in `@DBOS.step(...)` options. Decide whether it changes the step sequence. If it does, bump `APP_VERSION`.
- Expose new data through the step's return dict (it appears in `watch show` and `dbos workflow steps`) or through attributes (they appear in `watch jobs` through `_summary`).
- Document new environment variables in ADR 0002, the README *Watchers* section and this skill.

**Change the worker lifecycle:** keep the race handling. The worker re-checks `_has_work(include_running=False)` after it releases the lock, and `wake` checks the lock after a commit. Together they guarantee that some worker sees every committed job.

## Moving to Postgres or the cloud (planned, not built)

Follow *Path to the cloud* in ADR 0002. The seams are `Watchers.url` (one URL), the two `kbtd_*` tables (they become a `kbtd` schema), `_worker_lock` (it becomes queue partitions per board, `partition_concurrency=1`) and `emit_many` (it can become `DBOSClient.enqueue_in_transaction` once boards live in the same database).

## Packaging touchpoints

`dbos` is pinned in `pyproject.toml` and `uv.lock`. `bin/kbtd` (the mise launcher) syncs `uv.lock` into `<install>/.venv` with the uv build pinned in `mise.lock`. `package.nix` builds DBOS from its PyPI wheel, because nixpkgs has no package for it. Keep the wheel's version and hash in step with `uv.lock`.
