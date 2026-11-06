# Concepts

## Test, row and run

A **test** is one `[[test]]` block: one question for your agent.
Derail expands each test into **rows**:

| Row | Mocks | Fault |
|---|---|---|
| baseline | the test's mocks | none |
| fault 1 | the same mocks | the first `[[test.fault]]` |
| fault 2 | the same mocks | the second `[[test.fault]]` |

Faults are never combined, so a change in behaviour points to exactly one cause.
Baselines run first. If every baseline run of a test failed, its fault rows are skipped, because there is nothing to compare them with.

Each row runs `runs` times (3 by default).
A **run** is one start of your agent with the question.
Every run is a new Python program, so nothing leaks from one run to the next.

## Labels

A **label** names a call, such as `tool:get_order`.
This version knows these labels:

| Label | Means |
|---|---|
| `tool:<name>` | your agent's own Python tool, by the name the model sees |
| `tool:*` | every tool (only under `real`) |
| `model` | every chat call to a model, on any provider |
| `model:openai` | chat calls to OpenAI |
| `model:openai/embeddings`, `model:*/embeddings` | embedding calls |
| `hosted:mcp/<label>` | a hosted MCP server (only under `real`) |
| `http:<host>` | a web request to that host. See [Web requests](web-requests.md) |
| `mcp:<server>/<tool>`, `mcp:<tool>` | a tool on an MCP server. See [MCP tools](mcp.md) |
| `process:python` | a Python child that would run with no gates (only under `real`). See [An agent run as a script](scripts.md#a-python-child-derail-cannot-reach) |

## What a call can get

For every call, in this order:

1. **Fault**: if a fault names the call, it fires. Faults win over mocks, because a fault row is the baseline plus one fault.
2. **Mock**: if a mock names the call, the mock's value is returned and the real tool never runs.
3. **Real**: if the call is listed under `real`, the real tool runs.
4. **Reads**: a web `GET` or `HEAD` runs for real.
5. **Refused**: anything else is refused. See [Safety](safety.md).

## Mocks

A mock's `returns` is written in TOML.
If the tool returns its own type, Derail builds that type from the mock:

- the type is read from the tool's return annotation, such as `-> Stock`, or from the mock's `type`;
- a pydantic model is built with `model_validate`, any other class with `Type(**returns)`;
- a mock that does not fit the type is a mistake in the test.

## Faults

| Fault | What the agent sees |
|---|---|
| `error` | a tool raises an error: `ToolException` under LangChain, `RuntimeError` in a hand-written loop, or the type named by `raises`; an MCP tool returns a failed result, as a real server does |
| `timeout` | a tool raises `TimeoutError`; a model call, a web request or an MCP tool raises the library's own timeout error, as if the other side never answered |
| `empty` | an MCP tool returns a result with no content |
| `status` | written `status 429`: a model call gets a real HTTP error reply, and the library raises its own error, such as `RateLimitError`; a web request gets a reply with that status |

A fault fires on every call it names, unless `when` narrows it to the `first N` attempts or the first `requests N`. See [Failing the model](model-faults.md).
It fires only in the program Derail started, not in its child processes, unless `children` is set. See [An agent run as a script](scripts.md#faults-in-children).
If the agent never makes that call in a run, the run is **not exercised**: it was really a baseline run, so it is left out of the row's counts.
A row where the fault never fired tested nothing, and is reported as a mistake in the test.

## Records

Every call is written to the run's record, a JSON Lines file in `derail-runs/`.
Each line says which call it was, which process made it, what Derail did (real, mock, fault or refused), a hash, size and short preview of the arguments and result, and any error.
A call still going on when the run was stopped has a line whose error is `DerailStopped`.
A model call's line also holds the provider, the model, the status, the retry number, the token counts and the cost; its full request and reply are kept once in a `blobs/` folder next to the record, named by their hash.
Each process that joined a run has one line saying what it is: its interpreter, its command line and its parent's process id.
The last line is the run's summary: the answer, how the run ended, whether its fault fired, and what the run cost.

| How a run ended | Meaning |
|---|---|
| answered | the agent returned an answer, or a script gave one and exited with code 0 |
| crashed | the agent raised an error or exited with an error, or a script ended with no answer |
| timed out | the run passed `run_timeout` and was stopped |
| refused call | the agent or one of its child processes made a call the test does not allow, or a child on another Python imported a tool under `tools` |
| budget reached | the budget could not cover a model call, so the test stopped |
| infrastructure error | the record is incomplete, the agent's program or a child never got its plan, no Python claimed the run, or a model call failed by itself with nobody injecting it |
| stopped | another run stopped the whole test while this one was going, so it says nothing about this run |

Records can hold secrets and customer data, so they are readable only by you, and known secret patterns such as API keys are removed.
