# Tests

The suite has two tiers.

| Tier | What is in it | Command |
|---|---|---|
| fast | tests in one process: the ledger, the gates, the config, the report | `uv run pytest -m "not slow"` |
| slow | tests that start a process or a real `scrimmage test` run | `uv run pytest -m slow` |

A test that starts a process must be marked `@pytest.mark.slow`, or `pytestmark = pytest.mark.slow` for a whole file.
A fixture in `conftest.py` checks this: a test not marked slow that starts a process fails, naming the rule.

## While you work

Run the fast tier, then the slow tests of the area you changed:

```sh
uv run pytest -m "not slow" && uv run pytest -m slow tests/test_budget_wait.py
```

Before you push, run everything, as CI does:

```sh
uv run pytest
```

## Timing

A test never races the clock.
It waits for the event it means: a marker file the agent writes, a line in the record, the control server seeing an ask, or a fake clock.
The `time_limit` fixture makes a run's time limit arrive at such an event, as a short `run_timeout` races the program's start-up.
Where time itself is the claim, the margin is large enough to tell the two designs apart under any load, and a comment says so.
A wait for an event has a ceiling of 30 s or more, which only ends a test whose event never comes.
