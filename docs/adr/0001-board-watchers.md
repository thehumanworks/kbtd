# ADR 0001 — Board watchers and background shell actions

Accepted for the local CLI and TUI. [ADR 0003](0003-python-triggers.md) replaces the trigger syntax with Python expressions. [ADR 0002](0002-dbos-watcher-runtime.md) replaces the *Execution and recovery* and *Runner options* sections: DBOS now runs actions, reruns interrupted ones and keeps their output.

## Decision

A user registers a named shell command against a trigger for one resolved Markdown file path. The SQLite database under `$XDG_STATE_HOME/kbtd/watchers` holds the definitions and action history. The default state root is `~/.local/state`. Copying a board to another path or user account requires registering its watchers there. Sharing the Markdown shares the tasks. The first registration creates the database.

Both interfaces can identify the selected task and compare its fields during an edit. KBTD emits `owner.changed`, `tags.changed`, `task.completed` and `task.reopened` for transitions on existing tasks. Assignment, reassignment and removal all use `owner.changed`. Existing trailing `#tags` serve as labels. A watcher can filter on the resulting field value and column when deciding whether to enqueue its action.

One edit may change both owners and tags. The matcher evaluates each registered expression against all transitions and the snapshot of that one card. Each matching watcher receives one job per edit. Changing tag order alone produces zero jobs. Card paths identify positions in that edit’s snapshot. Watching arbitrary file edits would require resolving task identity across moves, renames and duplicate titles. KBTD therefore emits events from its own edits, with the TUI continuing to refresh on external saves.

## Action contract

The worker runs the registered command with the user's shell environment and the board's parent directory as its working directory. The command receives these additional environment variables.

| Variable | Contents |
| --- | --- |
| `KBTD_BOARD` | Absolute path of the Markdown file. |
| `KBTD_TRIGGER` | The registered event name or condition expression. |
| `KBTD_JOB_ID` | Board-local job number, stable across retries. |
| `KBTD_EVENT` | JSON object with the board, registered trigger, section and column names, their 1-based numbers, task path and before/after snapshots. A conditional trigger also includes an `events` array of the edit’s concrete event names. |

Each snapshot includes `text` (the task-line body), `done` (boolean), and sorted unique `owners` and `tags` arrays without their sigils. The [assignment log example](../../README.md#watchers) writes this object as a single line. A processor that needs deduplication can use the board path and job ID together. Task text is supplied as data through the environment. Quote environment expansions as shown in the example. Commands run with detached standard streams and can redirect output to files.

## Execution and recovery (superseded by ADR 0002)

After saving a board edit, KBTD commits all matching jobs to SQLite in one transaction. Each job captures its command and payload at enqueue time. Removing a definition affects future events. A short-lived worker runs queued jobs in ID order with one active worker per board. An operating-system-backed SQLite lock is released when the worker exits. The queue database remains available for edits while a command runs.

| State | Meaning |
| --- | --- |
| `queued` | The command is durably recorded and awaiting execution. |
| `running` | The worker claimed the job and incremented its attempt count. |
| `succeeded` / `failed` | The worker recorded the exit code or launch error. |
| `interrupted` | The worker disappeared during an attempt. Its effects may already have occurred. |

Opening the TUI, inspecting jobs or enqueuing a matching action wakes pending work. Recovery marks abandoned attempts as interrupted. Retrying a failed or interrupted job explicitly reruns the original command and payload. Watcher-initiated KBTD edits suppress further events via the inherited `KBTD_JOB_ID`.

The atomic Markdown replacement and SQLite commit are separate operations. A crash between them can leave a saved edit without a job. A dispatch error after a successful edit is reported as such. Only queued jobs have the recovery guarantee. Arbitrary shell effects also cannot be committed atomically with the job result.

## Runner options (superseded by ADR 0002)

| Option considered | Fit for KBTD |
| --- | --- |
| Python `sqlite3` plus a detached subprocess | Selected. Each action consists of one local command. SQLite supports transactional queue updates in a file without a server, and Python can launch and wait for the command. KBTD owns the small amount of claiming, recovery and reporting code. [Python SQLite documentation](https://docs.python.org/3.13/library/sqlite3.html) |
| DBOS with SQLite | DBOS can checkpoint multi-step Python workflows and recover at step boundaries with SQLite. That becomes useful if actions grow into multi-step workflows. A single shell command gives the framework no intermediate step to checkpoint. [DBOS Python guide](https://docs.dbos.dev/python/programming-guide) |
| Vercel Workflow SDK | Its workflow runtime and execution backends support durable steps and queues. Adopting that runtime would add integration work to run a shell in the user's local board directory. Hosted execution becomes relevant if actions move to shared infrastructure. [Vercel Workflow overview](https://vercel.com/blog/introducing-workflow) |
