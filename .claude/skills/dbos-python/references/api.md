# DBOS Python API reference (dbos 3.7)

The signatures below were printed from the installed package. Read `site-packages/dbos/_dbos.py` and `_client.py` when you need a parameter not listed here.

## Contents

1. Decorators
2. Context managers set around a call
3. In-process methods (`DBOS.*`)
4. Queues
5. `DBOSClient` (outside the application)
6. `WorkflowStatus` fields

## 1. Decorators

```python
@DBOS.workflow(name=None, max_recovery_attempts=100, serialization_type=None,
               validate_args=None, auto_upgrade=None)
@DBOS.step(name=None, retries_allowed=False, interval_seconds=1.0, max_attempts=3,
           backoff_rate=2.0, should_retry=None, preemptible=False, timeout_seconds=None)
```

- Give workflows an explicit, namespaced `name`. Clients enqueue by name, and two modules that register the same name raise `DBOSException`. The default name is `__qualname__`, without the module.
- `timeout_seconds` and `preemptible` apply only to `async def` steps.
- Both sync and async functions work. In async code, use the `_async` variants of the methods.

## 2. Context managers set around a call

```python
SetWorkflowID(wfid, *, workflow_id_reuse_policy=None)            # "return-existing" | "reject"
SetEnqueueOptions(*, deduplication_id=None, priority=None, app_version=None,
                  queue_partition_key=None, delay_seconds=None, duplication_policy=None)
SetWorkflowAttributes(attributes)                                  # JSON dict, set at creation
SetWorkflowTimeout(workflow_timeout_sec)                           # starts when dequeued
```

These context managers stack:

```python
with SetWorkflowID(str(job_id)), SetEnqueueOptions(priority=job_id), \
        SetWorkflowAttributes({"watcher": name}):
    DBOS.enqueue_workflow("jobs", my_workflow, arg, key=value)
```

## 3. In-process methods (after `DBOS(config=...)`)

| Call | Notes |
| --- | --- |
| `DBOS.launch()` | Starts the queue poller and the recovery thread. Call it after all decorators run. |
| `DBOS.destroy(*, destroy_registry=False, workflow_completion_timeout_sec=0)` | Stops background threads. After it, construct `DBOS(config=...)` again before relaunching. |
| `DBOS.migrate(url)` | Creates or upgrades the schema. It needs no instance. |
| `DBOS.listen_queues(names)` | Call it once, before launch, to dequeue only from these queues. |
| `DBOS.start_workflow(func, *a, **kw)` | Runs in the background and returns a `WorkflowHandle` (`get_status()`, `get_result()`, `workflow_id`). |
| `DBOS.enqueue_workflow(queue_name, func, *a, **kw)` | Enqueues a durable job and returns a handle. |
| `DBOS.workflow_id` | The current workflow ID inside a workflow or step. |
| `DBOS.get_workflow_status(id)` | Returns `WorkflowStatus` or `None`. |
| `DBOS.list_workflows(...)` | The same filters as the client (see section 5). |
| `DBOS.list_workflow_steps(id)` | Returns dicts with `function_id`, `function_name`, `output`, `error`, `started_at_epoch_ms`, `completed_at_epoch_ms`. |
| `DBOS.update_workflow_attributes(id, attrs, *, merge=False)` | Use `merge=True` to update some keys and keep the rest. |
| `DBOS.cancel_workflow(id)` | Stops the workflow at the start of its next step. A step that is running finishes. |
| `DBOS.resume_workflow(id, *, queue_name=None)` | Continues from the last completed step and resets the recovery count. |
| `DBOS.rewind_workflow(id, *, start_step=None, application_version=None, queue_name=None)` | Discards steps from `start_step` (1-based) onward and re-enqueues under the same ID. |
| `DBOS.fork_workflow(id, start_step)` | Copies the workflow to a new ID that starts at `start_step`. |
| `DBOS.recover_workflows(["executor-id"])` | Recovers another executor's workflows. Call it only when that executor is known to be dead. |
| `DBOS.sleep(seconds)` | A durable sleep that survives restarts. |
| `DBOS.send / recv / set_event / get_event / write_stream / read_stream` | Communication with workflows. See `tutorials/workflow-communication` in the docs. |

## 4. Queues

```python
DBOS.register_queue(name, *, global_concurrency=None, worker_concurrency=None, limiter=None,
                    partition_concurrency=None, partition_worker_concurrency=None,
                    partition_limiter=None, polling_interval_sec=1.0,
                    on_conflict="update_if_latest_version")
```

- Call it after `launch()`. The configuration is stored in the database, so `register_queue` on startup can overwrite changes that someone made at runtime.
- `worker_concurrency=1` runs one job at a time per process. With one process, that is strict sequential execution.
- `global_concurrency=1` runs one job at a time across all processes. Every `PENDING` workflow counts against the limit, including a stranded one.
- In a partitioned queue, every enqueue must set `queue_partition_key`. A job without one stays `ENQUEUED`. Partitions do not support deduplication.
- `polling_interval_sec` is the minimum time between polls, plus jitter. It sets the latency from enqueue to start.

## 5. `DBOSClient` (outside the application)

```python
client = DBOSClient(system_database_url=url, application_name="my-app")   # connects immediately
handle = client.enqueue({"queue_name": "jobs", "workflow_name": "app.notify",
                         "workflow_id": "42", "priority": 42,
                         "serialization_type": WorkflowSerializationFormat.PORTABLE}, "a@example.com")
client.list_workflows(workflow_ids=None, status=None, name=None, app_version=None,
                      queue_name=None, limit=None, offset=None, sort_desc=False,
                      load_input=True, load_output=True, attributes=None, ...)
client.list_queued_workflows(...)
client.list_workflow_steps(id, *, load_output=True, limit=None, offset=None)
client.retrieve_workflow(id)               # raises DBOSNonExistentWorkflowError when the ID is unknown
client.cancel_workflow(id, *, cancel_children=False)
client.resume_workflow(id, *, queue_name=None)
client.rewind_workflow(id, *, start_step=None, application_version=None, queue_name=None)
client.enqueue_in_transaction(conn, options, *args)   # atomic with your own SQL (outbox pattern)
client.destroy()                           # close the connection pool
```

- Always set `application_name` when several applications share a database.
- Calling a client method inside a workflow records it as a step.
- `status` accepts a string or a list: `PENDING`, `ENQUEUED`, `DELAYED`, `SUSPENDED`, `SUCCESS`, `ERROR`, `CANCELLED`, `MAX_RECOVERY_ATTEMPTS_EXCEEDED`.

## 6. `WorkflowStatus` fields

`workflow_id`, `status`, `name`, `input` (args/kwargs), `output`, `error`, `attributes`, `recovery_attempts`, `queue_name`, `priority`, `executor_id`, `app_version`, `application_name`, `created_at`, `updated_at`, `dequeued_at`, `completed_at` (epoch milliseconds), `deduplication_id`, `workflow_timeout_ms`, `parent_workflow_id`, `forked_from`.

`recovery_attempts` counts executions since the last rewind or resume, including replays. It is **not** the number of times a side effect ran. Count side effects inside the step, for example in an attribute.
