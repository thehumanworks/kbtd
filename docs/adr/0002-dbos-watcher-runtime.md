# ADR 0002 — DBOS runs watcher actions

Accepted. This record replaces the runner, recovery and runner-options parts of [ADR 0001](0001-board-watchers.md). The trigger model, the event payload and the action environment of ADR 0001 stay in force, with the additions below.

## Context

ADR 0001 chose a hand-written SQLite queue and a detached worker. That worker marked an action as `interrupted` when it stopped, and never ran it again by itself. Job history was visible only through `kbtd watch jobs`, and the command's output was discarded.

We want three things:

1. **Durability.** An action that a reboot or a killed worker interrupts should finish without the user noticing.
2. **Observability.** A user or an agent should see what ran, when, how many times, and why it failed.
3. **A path to the cloud.** Later, `TODO.md` and the job history may live in shared storage, with workers on other machines. We do not build that now, but the choice must not block it.

## Options compared

Both frameworks were read from their `llms.txt` documentation indexes and then tried in a scratch environment with the versions current on 2026-11-26.

| Question | DBOS Transact for Python 3.7 on SQLite | Vercel Workflow SDK for Python 0.13 |
| --- | --- | --- |
| Status | Generally available. Python is a first-class SDK. | Beta: "APIs and behavior may change". TypeScript is the primary SDK. |
| Runs without a server | Yes. A library plus one SQLite file. `DBOSClient` reads and writes jobs from any process without launching DBOS. | Only while the process that started the run stays alive. The local queue is in memory. A run started by a process that then exits stayed `pending` and no later process picked it up (tested). |
| Fits a short-lived CLI | A detached worker launches DBOS, recovers interrupted jobs, drains the queue and exits (tested). | Needs a long-running app server with an HTTP route that receives queue deliveries. |
| Recovery | Steps run at least once and never again after they complete. A worker that launches re-enqueues the `PENDING` jobs of its executor. `max_recovery_attempts` caps reruns (tested). | Deterministic replay of an event log. Durable on the Vercel World. The self-hosted Postgres World is a TypeScript package. |
| Inspection | `dbos workflow list/get/steps`, `DBOSClient`, attributes per job, readable JSON with portable serialization, optional OpenTelemetry export and the Conductor console. | `npx workflow inspect` and `npx workflow web` (requires Node.js), or the Vercel dashboard. |
| Scale-out | Change the URL to Postgres. Queues support global and per-partition concurrency. Any host. Optional Conductor or DBOS Cloud. | Zero configuration on Vercel. The Python SDK authenticates to Vercel Queues with Vercel OIDC, which ties Python workers to Vercel. |
| Install footprint | 11 packages, about 40 MB (SQLAlchemy, psycopg, croniter, click, PyYAML). | 28 packages, about 53 MB (pydantic, cryptography, httpx, Vercel clients). |

**Decision: DBOS with a SQLite system database.** It runs where kbtd runs today, it adds recovery and inspection that kbtd would otherwise have to write, and moving to Postgres changes a URL, not the code. The Vercel SDK is a better fit for an app deployed on Vercel. kbtd is a local CLI first.

## Design

### One database file per board

The watcher database stays at `$XDG_STATE_HOME/kbtd/watchers/<sha256 of the board path>.sqlite3`. It is the DBOS system database, and kbtd keeps two tables of its own in it:

| Table | Contents |
| --- | --- |
| `kbtd_watchers` | Watcher definitions (name, trigger, command). A `watchers` table from ADR 0001 is renamed on first use. |
| `kbtd_outbox` | Jobs that an edit has committed but the worker has not yet handed to DBOS. |

One file keeps the definitions, the outbox and the job history consistent with each other. On Postgres the same layout becomes a `kbtd` schema next to the `dbos` schema. A `.lock` file marks the live worker, and a `.log` file collects worker diagnostics.

### Edits stay on the standard library

A warm `import dbos` takes about 0.45 s. Recording a job with `sqlite3` takes about 2 ms. So the edit path never imports DBOS:

1. A CLI or TUI save evaluates triggers and inserts every matching job into `kbtd_outbox` in one transaction. `AUTOINCREMENT` gives each job a board-local number that is never reused.
2. It wakes a worker if no worker holds the lock.

`kbtd watch add` imports DBOS and migrates the database. A missing or broken DBOS install therefore fails at registration, not later on an edit. Jobs recorded while the worker cannot start stay in the outbox and run later.

### The worker

The worker starts in an isolated interpreter (`python -I`) with the caller's import path, minus the current directory. Modules in the board directory never load. It then:

1. Takes the lock or exits. With the lock held, every `PENDING` job belongs to a worker that stopped, so DBOS recovery at launch is safe. DBOS warns that recovering a live executor runs steps twice.
2. Launches DBOS with application version `"1"`, and registers the `kbtd-watchers` queue with `worker_concurrency=1`.
3. Moves outbox rows into DBOS in job order. The workflow ID is the job number. A repeated handoff after a crash returns the existing workflow. The priority is also the job number, because DBOS dequeues by priority and then creation time, and several jobs can share a millisecond.
4. Exits after two consecutive idle checks, then releases the lock. If a job arrived after the last check, it wakes a new worker.

### The workflow

`kbtd.watcher_action` has one step, `kbtd.run_command`. The step increments a run attribute, `attempt`, and runs the command. It returns the exit code and the last 8 KiB of combined stdout and stderr. The output goes to a temporary file, not a pipe, so a background process that inherits it cannot hold the job open. A non-zero exit raises `ActionFailed`, so DBOS records the job as `ERROR` and DBOS tools show it as a failure.

The workflow uses DBOS portable JSON serialization. Inputs, outputs, errors and attributes are plain JSON in the database, readable by any DBOS SDK, by SQL, or by a future web view. No pickle is stored, which matters once a database is shared.

### Statuses and retries

| kbtd status | DBOS status |
| --- | --- |
| `queued` | Outbox row, `ENQUEUED` or `DELAYED` |
| `running` | `PENDING` (also while it waits for recovery) |
| `succeeded` | `SUCCESS` |
| `failed` | `ERROR` |
| `interrupted` | `MAX_RECOVERY_ATTEMPTS_EXCEEDED` |
| `cancelled` | `CANCELLED`, for example from `dbos workflow cancel` |

`kbtd watch retry ID` rewinds a failed, interrupted or cancelled job to its first step. It keeps the job number and its captured command and payload, and returns it to the queue. Rewinding resets the DBOS recovery counter but keeps the `attempt` attribute, so `ATTEMPT` counts every start of the command.

`kbtd watch cancel ID` and the TUI's job view cancel a job only while DBOS holds it as `ENQUEUED` or `DELAYED`, with `DBOSClient.cancel_workflow`. A job still in `kbtd_outbox` is refused rather than deleted, because deleting the row would race the worker's handoff. DBOS checks for cancellation only between steps, so cancel cannot stop a running command, and a job dequeued between the status check and the cancel may still run. A cancelled job can be retried like a failed one.

## Consequences

- **Actions run at least once.** If the worker stops during an action, the next worker runs the action again. After three starts DBOS stops and the job becomes `interrupted`. ADR 0001 never reran automatically. `KBTD_ATTEMPT` (1-based) lets a command detect a rerun; `KBTD_BOARD` plus `KBTD_JOB_ID` remain the deduplication key.
- **Output is kept.** `kbtd watch show ID` prints the job as JSON, with its event, exit code and output tail. Commands that redirect their own output behave as before.
- **Standard DBOS tools work on the file.** For example, `dbos workflow steps 3 -s sqlite:///<path>` lists a job's steps.
- **kbtd needs `dbos` to run watchers.** It is a declared dependency. The board commands and the TUI still work without it, and jobs still queue.
- **Latency.** A worker needs about one second to start, and the queue polls every 0.1 s. Edits are not slower.
- **Upgrades.** Bump `APP_VERSION` only when the workflow's steps change. A worker runs only jobs of its own version, or unversioned jobs.
- **Unchanged.** As in ADR 0001, the Markdown replacement and the outbox commit are separate operations. A crash between them can leave a saved edit without a job.

## Path to the cloud (not built)

1. Point `Watchers.url` at Postgres, and move the two kbtd tables into a `kbtd` schema. Nothing else in the workflow changes.
2. Replace the per-board lock with one queue partition per board (`partition_concurrency=1`). Jobs of one board then stay in order while any number of workers serve all boards.
3. Give each worker a stable `executor_id`, or connect Conductor, so that a dead worker's jobs are recovered by another machine.
4. If boards move into the database, an edit and its jobs can commit in one transaction with `DBOSClient.enqueue_in_transaction`. That closes the gap in the last consequence above.
