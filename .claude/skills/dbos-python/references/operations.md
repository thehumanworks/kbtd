# Operating DBOS: inspection, recovery, upgrades, scale-out

## Statuses

| Status | Meaning | What to do |
| --- | --- | --- |
| `ENQUEUED` / `DELAYED` | Waiting on a queue (`DELAYED`: until `delay_seconds` passes). | It runs when an executor of its version polls that queue. |
| `PENDING` | Running, or its executor died. | It recovers on the next launch of the same `executor_id`, or with `recover_workflows`. |
| `SUCCESS` / `ERROR` | Finished. `ERROR` means an exception escaped the workflow. | Rewind or fork it to run it again. |
| `CANCELLED` | Cancelled. Cancellation takes effect at the next step boundary. | Resume or rewind it. |
| `MAX_RECOVERY_ATTEMPTS_EXCEEDED` | Dead letter: too many crash recoveries. | Fix the cause, then resume or rewind it. |

## The `dbos` CLI

Every command takes `-s/--sys-db-url URL`, or reads `system_database_url` from `dbos-config.yaml`. Output is JSON.

```sh
dbos workflow list -s sqlite:////abs/app.sqlite3 -l 20 -d      # newest first
dbos workflow list -s ... -S ERROR -n app.notify
dbos workflow get 42 -s ...
dbos workflow steps 42 -s ...                                    # step outputs and errors
dbos workflow queue list -s ... -q jobs
dbos workflow cancel|resume 42 -s ...
dbos workflow fork 42 -S 2 -s ...                                # new ID, restart at step 2
dbos migrate -s URL                                              # create or upgrade the schema
dbos reset -y -s URL                                             # deletes the SQLite file and every table in it
```

Outputs are readable only when the workflow used portable serialization. Pickled values print as base64.

Plain SQL also works on the documented tables: `workflow_status` (one row per workflow), `operation_outputs` (one row per step), `workflow_input`, `workflow_output`, `queues`. Read them freely. Write only through DBOS APIs, except the documented `dbos.enqueue_workflow` PL/pgSQL function on Postgres.

## Recovery rules

- On launch, an executor re-enqueues its own `PENDING` workflows (same `executor_id` and version) onto their original queues, in a background thread.
- Recovery is safe only if the old process is gone. Otherwise steps run twice, although DBOS still records one outcome. Keep `executor_id` unique per live process, or hold an OS-level lock.
- `max_recovery_attempts` (default 100) bounds crash loops. For shell or email side effects, a small number (2–3) plus an attempt counter that the action can read is kinder.

## Upgrading code

- DBOS runs a workflow only on executors of its `application_version`. Pin the version in config. Bump it when a change would make the replay of a recorded run call different steps: renaming, reordering, inserting or removing steps, or changing a step's inputs.
- After a bump, old `PENDING` workflows stay stuck until an executor of the old version finishes them, or until you `rewind_workflow(id, application_version="new")` or fork them.
- `ENQUEUED` workflows enqueued without a version run on the latest version.

## Logging and tracing

- `log_level` in the config controls the `dbos` logger. DBOS logs every exception that escapes a background workflow at ERROR level, with a traceback. Add a `logging.Filter` on the `dbos` logger to drop expected failures.
- With `enable_otlp=True` and `otlp_traces_endpoints=[...]` (extra `dbos[otel]`), DBOS creates spans for every workflow and step. To join a caller's trace, use `PropagateOtelContext`, or the `otel_context` option when enqueuing from a client.
- Workflow attributes (`SetWorkflowAttributes`, `update_workflow_attributes`) are the place for tenant, job or counter metadata. The CLI and the console show them. Filtering by attribute works only on Postgres.

## SQLite to Postgres

1. Use `system_database_url="postgresql://user:pass@host:5432/db"`. DBOS always uses psycopg 3. Percent-encode special characters in the password. DBOS tables go in schema `dbos` (`dbos_system_schema`).
2. Keep the same code. Postgres adds LISTEN/NOTIFY wakeups (`use_listen_notify`), filtering by attribute, and safe use by many executors.
3. Give each process a stable, unique `executor_id`, or connect Conductor (`conductor_key`, extra `dbos[enterprise]`). Conductor detects dead executors and recovers their workflows elsewhere.
4. Replace per-process locks with queue flow control. For example, `partition_concurrency=1` with `queue_partition_key=<tenant>` gives one job at a time per tenant across the fleet.
5. If your application data lives in the same Postgres, `DBOSClient.enqueue_in_transaction(conn, ...)` makes "save the data and enqueue the job" one atomic commit.
