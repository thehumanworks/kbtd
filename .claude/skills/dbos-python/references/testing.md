# Testing DBOS code

## Isolate the system database per test

DBOS is a process-wide singleton. Use a fresh SQLite file (or `reset_system_database`) for each test, and destroy DBOS between tests:

```python
class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        DBOS(config={"name": "test-app", "application_version": "1",
                     "system_database_url": f"sqlite:///{self.dir / 'sys.sqlite3'}"})
        DBOS.launch()
        DBOS.register_queue("jobs", polling_interval_sec=0.05)   # fast polling in tests
        self.addCleanup(DBOS.destroy)
```

Keep `destroy_registry=False`, the default. Otherwise decorated functions disappear for the next test. `DBOS.reset_system_database(system_database_url=..., truncate=True)` empties the tables quickly, but only while DBOS is not launched.

## Wait on state, not on time

Background workflows finish asynchronously. Poll a status with a deadline instead of sleeping for a fixed time:

```python
def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")

wait_for(lambda: DBOS.get_workflow_status("42").status == "SUCCESS")
```

`handle.get_result()` also blocks until the workflow finishes, and re-raises its error.

## Simulate crashes realistically

Kill a real process. Faking it by setting a row to `PENDING` skips the parts that matter: the lock release, startup recovery, and the at-least-once rerun of a step.

1. Run the worker as a subprocess started with `start_new_session=True`. It then leads its own process group.
2. Have the step's action write its `os.getpgid(0)` to a file, then block.
3. Use `os.killpg(pgid, signal.SIGKILL)` to stop the worker and the action together, as a reboot would.
4. Wait until the worker has really exited (for example until its lock can be taken), then start a new worker. Assert that the step ran again (an attempt counter) and that the final status is right.
5. Repeat until the run passes `max_recovery_attempts`, and assert `MAX_RECOVERY_ATTEMPTS_EXCEEDED`.

kbtd's `tests/test_watchers.py::test_stopped_worker_reruns_the_action_until_the_recovery_limit` is a working example.

## Cost

Each new process pays about 0.5 s to import DBOS, plus the launch time. Keep tests that spawn workers few and focused, and test pure logic (conditions, payloads) without DBOS.
