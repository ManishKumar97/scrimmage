# Concepts

## Test, row and run

A **test** is one `[[test]]` block: one question for your agent.
Faultline expands each test into **rows**:

| Row | Mocks | Fault |
|---|---|---|
| baseline | the test's mocks | none |
| fault 1 | the same mocks | the first `[[test.fault]]` |
| fault 2 | the same mocks | the second `[[test.fault]]` |

Faults are never combined, so a change in behaviour points to exactly one cause.

Each row runs `runs` times (3 by default).
A **run** is one start of your agent with the question.
For a function agent, every run is a new Python program, so nothing leaks from one run to the next.

## Labels

A **label** names a call, such as `tool:get_order`.
This version knows one kind of label:

| Label | Means |
|---|---|
| `tool:<name>` | your agent's own Python tool, by the name the model sees |
| `tool:*` | every tool (only under `real`) |

Model calls, web requests and MCP tools get their own labels in later versions.

## What a call can get

For every call, in this order:

1. **Fault**: if a fault names the call, it fires. Faults win over mocks, because a fault row is the baseline plus one fault.
2. **Mock**: if a mock names the call, the mock's value is returned and the real tool never runs.
3. **Real**: if the call is listed under `real`, the real tool runs.
4. **Refused**: anything else is refused. See [Safety](safety.md).

## Mocks

A mock's `returns` is written in TOML.
If the tool returns its own type, Faultline builds that type from the mock:

- the type is read from the tool's return annotation, such as `-> Stock`, or from the mock's `type`;
- a pydantic model is built with `model_validate`, any other class with `Type(**returns)`;
- a mock that does not fit the type is a mistake in the test.

## Faults

| Fault | What the agent sees |
|---|---|
| `timeout` | the tool raises `TimeoutError`, as if the other side never answered |

A fault fires on every call it names (`when = "always"`).
If the agent never makes that call in a run, the run is **not exercised**: it was really a baseline run, so it is left out of the row's counts.
A row where the fault never fired tested nothing, and is reported as a mistake in the test.

## Records

Every call is written to the run's record, a JSON Lines file in `faultline-runs/`.
Each line says which call it was, what Faultline did (real, mock, fault or refused), a hash, size and short preview of the arguments and result, and any error.
The last line is the run's summary: the answer, how the run ended, and whether its fault fired.

| How a run ended | Meaning |
|---|---|
| answered | the agent returned an answer |
| crashed | the agent raised an error or exited with an error |
| timed out | the run passed `run_timeout` and was stopped |
| refused call | the agent made a call the test does not allow |
| infrastructure error | the record is incomplete, or the agent's program never got its plan |

Records can hold secrets and customer data, so they are readable only by you, and known secret patterns such as API keys are removed.
