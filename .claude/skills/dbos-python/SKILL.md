---
name: dbos-python
description: Build, debug and operate durable workflows with DBOS Transact for Python (the `dbos` package), especially on a SQLite system database. Use this skill whenever code imports `dbos`, uses `@DBOS.workflow`, `@DBOS.step`, `DBOS.enqueue_workflow`, `DBOSClient` or `DBOS.launch`, or when a task asks for background jobs, durable execution, crash recovery, retries, queues, idempotent side effects or workflow inspection in Python, even if DBOS is not named. Also use it to read a DBOS system database (`workflow_status`, `operation_outputs`) or run `dbos workflow ...` commands.
---

# DBOS for Python

DBOS is a library, not a service. Workflows and steps are ordinary decorated Python functions. DBOS checkpoints each step's result in a **system database** (SQLite or Postgres). After a crash, a process that launches DBOS re-runs each unfinished workflow from the start and replays recorded step results. Execution resumes at the first step that did not finish.

These notes target `dbos` 3.7. Facts marked *(verified)* were tested against that version. Everything else comes from https://docs.dbos.dev (index: https://docs.dbos.dev/llms.txt). When the docs and these notes disagree, check the installed source in `site-packages/dbos/`. The API moves quickly, and older blog posts show older APIs (for example `Queue("name")` objects and `DBOS()` configured by YAML).

## Mental model

| Concept | What it guarantees |
| --- | --- |
| Workflow (`@DBOS.workflow()`) | Runs to completion across crashes. Its body must be **deterministic**: the same inputs and step results must lead to the same steps in the same order. |
| Step (`@DBOS.step()`) | Runs **at least once**. It never runs again after its result is recorded. Put every side effect and every non-deterministic call (I/O, clock, random, subprocess) in a step. |
| Workflow ID | The idempotency key. Starting or enqueuing an existing ID returns the existing workflow by default (`workflow_id_reuse_policy="return-existing"`). |
| Queue | Durable FIFO with flow control (`worker_concurrency`, `global_concurrency`, rate limits, partitions). Persisted in the database, so any process or client can enqueue. |
| Executor | One process running DBOS (default `executor_id` is `"local"`). On launch it recovers its own `PENDING` workflows. |
| Application version | Workflows run only on executors of their version. Without a version set, DBOS hashes the workflow source, so any code change makes a new version. |

## Minimal SQLite setup

```python
from dbos import DBOS, DBOSConfig

@DBOS.step()
def send(to: str) -> dict:
    ...                      # side effect lives in a step

@DBOS.workflow(name="app.notify")   # stable name: clients enqueue by name
def notify(to: str) -> dict:
    return send(to)

DBOS(config={
    "name": "my-app",                         # 3-256 chars: lowercase, digits, - and _
    "system_database_url": "sqlite:////abs/path/app.sqlite3",
    "application_version": "1",               # pin it; see "Versions" below
    "log_level": "WARNING",
})
DBOS.launch()                                  # decorators must run before this
DBOS.register_queue("jobs", worker_concurrency=1)   # register queues AFTER launch
DBOS.enqueue_workflow("jobs", notify, "a@example.com")
```

`sqlite:///` plus an absolute path gives four slashes. Without a URL, DBOS creates `<name>.sqlite` in the current directory.

## Decisions that are easy to get wrong

1. **Where the step boundary goes.** Anything outside a step re-executes on every recovery. A step that does two side effects can repeat the first after a crash in the second. Use one step per side effect, and make steps idempotent or pass the workflow ID downstream as a deduplication key.
2. **Pin `application_version`.** The default source hash strands `PENDING` workflows after every upgrade, because a new version does not run them. Bump the pinned version only when the step sequence of a workflow changes.
3. **Recover only executors that are really dead.** Launch recovers every `PENDING` workflow of this `executor_id`. Two live processes with the same executor ID run the same steps twice. Use distinct, stable executor IDs, or a lock that proves the previous process exited.
4. **Choose serialization deliberately.** The default is pickle. `@DBOS.workflow(serialization_type=WorkflowSerializationFormat.PORTABLE)` stores inputs, outputs and errors as JSON. Other languages, SQL and the `dbos` CLI can then read them, and a shared database holds no pickles. Portable mode restricts values to JSON types.
5. **Failures are workflow outcomes.** An exception that escapes the workflow sets status `ERROR`, and DBOS does not recover it. Use step retries (`retries_allowed=True, max_attempts=...`) for transient errors. Raise when a failure should show as `ERROR` in tools.
6. **Keep imports out of hot paths.** A warm `import dbos` costs about 0.45 s (SQLAlchemy) *(verified)*. A latency-sensitive CLI can write intents with `sqlite3` and let a worker enqueue them. This is the transactional-outbox pattern.

## Verified behaviors (dbos 3.7)

- `DBOS.migrate(url)` creates the schema without launching. `DBOSClient` needs the schema to exist.
- `DBOSClient(system_database_url=..., application_name=...)` enqueues, lists, rewinds and cancels from any process without `DBOS.launch()`.
- Dequeue order is `priority ASC, created_at ASC`. Jobs created in the same millisecond have no defined order. Set `SetEnqueueOptions(priority=n)` with a sequence number for strict order (1 to 2^31-1; lower runs first).
- Startup recovery re-enqueues `PENDING` workflows on **their original queue** in a background thread, so queue concurrency limits still apply.
- `max_recovery_attempts=N` allows N automatic recoveries: N+1 executions in total. The next launch then marks the workflow `MAX_RECOVERY_ATTEMPTS_EXCEEDED`.
- Step numbers start at 1. `rewind_workflow(id, start_step=1)` reruns from the first step, keeps the ID, the inputs and the attributes, resets `recovery_attempts`, and re-enqueues on the original queue. It works on `ERROR`, `CANCELLED` and `MAX_RECOVERY_ATTEMPTS_EXCEEDED`.
- `list_workflows(load_output=False)` also leaves `error` as `None`. Load outputs when you need the error.
- A portable error returns as `PortableWorkflowError`. Read `.message`; `str(error)` can return `"None"`.
- Inside a step, `DBOS.workflow_id`, `DBOS.get_workflow_status(id)` and `DBOS.update_workflow_attributes(id, {...}, merge=True)` work. Use them for counters that must count real executions.
- `DBOS.destroy(workflow_completion_timeout_sec=N)` stops queue polling and waits up to N s for running workflows. A workflow still running when the process exits recovers as `PENDING` later.
- kbtd's own tables can share the SQLite file with DBOS. DBOS uses a rollback journal with `busy_timeout=30000`, and concurrent `sqlite3` writers work.

## Where to go next

- `references/api.md` — signatures for workflows, steps, context managers, queues and `DBOSClient`.
- `references/operations.md` — statuses, the `dbos` CLI, recovery, upgrades, tracing, and moving from SQLite to Postgres.
- `references/testing.md` — test isolation, re-launching, and simulating crashes.
