# Benchmarks

These are real measurements, all from one machine.
Treat them as a guide to the size of each cost, not as a promise about yours.

**Machine:** a Microsoft Azure `Standard_D4as_v4` virtual machine, x86_64, with 4 vCPUs and Linux, running Python 3.11.17, otherwise idle.

**To reproduce them,** from the root of a clone, after `uv sync`:

```sh
uv run python tests/benchmark.py
```

The command prints every table below.
`--rounds N` sets the repeats (5 by default); `--section calls` runs one measure at a time.
Run it on an otherwise idle machine.

A local server in its own process takes the place of the network, so network time is not in any number.
Gated calls go through the real gates, a real control server and the real sender thread.
Each measure was repeated 5 times; the range after each figure is the spread between those repeats.
The first calls of each kind, which set up connections, are left out.
The web reply is about 1 KB, and the model reply is 796 bytes.

## Time added per gated call

Each row is the same call made with Scrimmage and without it, one after the other.
The table gives the time added at the median and at the 99th percentile, in microseconds.

| Call | Ungated p50 | Gated p50 | Added p50 (spread) | Ungated p99 | Gated p99 | Added p99 (spread) |
|---|---|---|---|---|---|---|
| Tool call, sync | 0.2 | 43.3 | 43.1 (42.6 to 43.5) | 0.2 | 102.1 | 101.9 (91.3 to 109.6) |
| Tool call, async | 0.3 | 43.3 | 43.0 (42.8 to 43.6) | 0.3 | 96.7 | 96.4 (86.7 to 105.6) |
| Web request through httpx, sync | 357.9 | 673.6 | 306.8 (280.5 to 320.5) | 514.9 | 1095.6 | 585.4 (523.0 to 1017.2) |
| Web request through httpx, async | 697.8 | 1074.6 | 372.8 (348.5 to 397.1) | 941.0 | 1508.6 | 600.0 (488.9 to 1131.8) |
| Model call, sync, budget on | 1231.0 | 2890.4 | 1645.5 (1610.1 to 1696.6) | 1383.4 | 3867.7 | 2484.3 (2081.9 to 3363.0) |
| Model call, async, budget on | 1543.7 | 3640.7 | 2071.2 (2036.5 to 2121.0) | 1761.0 | 5633.1 | 3962.1 (3476.7 to 4301.5) |
| Model call, sync, budget off | 1231.0 | 1730.8 | 499.8 (464.5 to 540.5) | 1383.4 | 2442.2 | 1069.4 (514.7 to 1173.3) |
| Model call, async, budget off | 1543.7 | 2155.2 | 614.2 (531.1 to 621.6) | 1761.0 | 2762.6 | 1047.4 (881.6 to 2331.6) |
| MCP tool call, stdio, async | 1305.5 | 1564.3 | 257.3 (220.9 to 270.3) | 1444.6 | 2198.2 | 725.0 (628.4 to 1004.7) |

A mocked call makes no real call, so it has no ungated row to compare with.
A mocked tool call takes 48.0 µs at the median, and a mocked web request 185.5 µs.
A call that misses a mock with `args`, on a tool under `real` by name, runs for real and adds about 126 µs, as it looks for the nearest mock and keeps the evidence.

A gated model call with a budget on makes one extra signed request to the runner, to reserve the call's money.
That is the largest part of the time it adds.

A real provider's response time was not measured here.
If a model call takes 200 ms, 2 ms adds 1 percent.

The MCP library has no sync client, so MCP has only an async row.
The MCP rows use the stdio transport.

## Not measured

- Injected fault paths.
- Streamed responses, on web requests or model calls.
- MCP over HTTP.
- Bodies that contain secrets.
- A real provider's latency.

## Agent startup

The time from just before the process starts to its first line of code, and to its exit, for a program that does nothing.
Each column is in milliseconds, against plain Python.

| Process | First line (spread) | Added | Exit (spread) | Added |
|---|---|---|---|---|
| Plain Python | 9.1 (9.0 to 9.3) | 0 | 11.0 (10.9 to 11.1) | 0 |
| Hooks loaded, root process | 77.5 (77.0 to 78.1) | 68.2 | 92.4 (91.7 to 92.7) | 81.3 |
| Hooks loaded, child process | 75.4 (74.9 to 75.7) | 66.3 | 87.6 (87.1 to 88.6) | 76.7 |
| Through `scrimmage-run`, the real path | 98.8 (98.1 to 99.2) | 89.5 | 113.3 (113.0 to 113.9) | 102.2 |

A run's root process wakes the thread that sends records when it exits, so its last records go at once and it does not wait for that thread's 100 ms window.
A child process sends each record as it is made, so it has none left to send.

## CLI

Wall time in milliseconds, with the median of 5 repeats of 5 commands each.

| Command | Wall ms (spread) | Added to plain Python |
|---|---|---|
| Plain Python | 11.2 (11.1 to 11.9) | 0 |
| Import of `scrimmage_core.cli` | 56.4 (56.1 to 57.0) | 45.2 |
| `scrimmage --help` | 59.9 (59.8 to 61.5) | 48.7 |
| `scrimmage check` on the example | 60.2 (59.6 to 62.0) | 49.0 |

## Runner overhead per run

For an agent that does nothing, started one at a time, in milliseconds.
The cost per run comes from the slope between a test of 1 run and a test of 11 runs.

| Measure | ms (spread) |
|---|---|
| Whole cost of one run | 162.0 (157.2 to 162.1) |
| The same agent under plain Python | 11.9 (11.8 to 12.4) |
| Added per run by the runner and hooks | 150.1 (145.1 to 150.3) |
| Fixed cost of one `scrimmage test` | 541.1 (539.2 to 544.0) |

## Budgets

The suite's slow tier has a test, `tests/test_benchmark_budgets.py`, that fails when a cost goes over its ceiling.
The ceilings are set far above today's quiet numbers, and above the worst seen on a machine under heavy load.
That is 11.6 times for a tool call, 19.6 times for a sync web request, 18.8 times for an async one, 23.3 times for an MCP call, 12.2 times for a sync model call, 9.7 times for an async one, and 10.6 to 12.3 times for the CLI.
So they do not fail because the machine is busy, and they catch only a large slowdown.
Of the per-call ceilings, only the tool call's catches one added round trip to the runner.
A web request needs about 5.7 ms added to fail, and a model call about 18 ms.
The wait at exit is not caught by a ceiling.
A test that sets the sender's window to an hour catches it, with no clock.

The same test file has two checks that use no clock.
One counts the requests to the runner that a gated call waits on, as they go on the wire: none for a tool, a web request or an MCP call, one for a model call with a budget, and none for a model call without one.
That holds for a program's root process, whose record lines go through a sender thread.
A child process sends each line as it is made, and the call waits on that request.
The count leaves the sender thread out, and the child case is not measured here.
The other two import `scrimmage_core.cli` and `scrimmage_core.bootstrap` in new processes.
The first checks that the runner, `openai`, `httpx`, `importlib.metadata` and the modules the runner brings are not loaded.
The second checks that the SDK has not loaded `asyncio`, `importlib.metadata`, `concurrent.futures`, `logging`, `openai` or `httpx`.

Nothing enforces a smaller slowdown.

| Measure | Ceiling |
|---|---|
| Tool call, added | 500 µs |
| Tool call that misses a mock, added | 1.5 ms |
| Web request, added | 6 ms sync, 7 ms async |
| Model call with a budget, added | 20 ms |
| MCP tool call, added | 6 ms |
| Startup, added to the first line | 800 ms |
| Startup, added to process exit | 1150 ms |
| `scrimmage check`, added | 520 ms |
| `scrimmage --help`, added | 600 ms |
| Import of `scrimmage_core.cli`, added | 500 ms |
| One run, added | 1450 ms |

## What the test suite already shows

These are not timings.
Each one is a check, in the repository's test suite, that an injected failure reaches the agent the same way a real one does.
Each runs on a fake model server, with no API key.
Run each command from the root of a clone, after `uv sync`.

| Check | What it shows | Command |
|---|---|---|
| A real tool timeout against an injected one | A `get_order` that really times out against a system that never answers, and one with a `timeout` fault, raise the same error type into the agent's loop, and the loop tells the model the same thing | `uv run pytest -n0 tests/test_real_against_injected.py` |
| A real HTTP 429 against an injected one | A 429 from the model server and a `status 429` fault end the same way in the agent, on a plain call and on a streamed one | `uv run pytest -n0 "tests/test_model_faults.py::test_a_real_429_and_the_injected_one_end_the_same_way"` |
| A real LangChain tool error against an injected one | A tool that really raises `ToolException`, and an `error` fault, give the same outcome, with and without error handling in the graph | `uv run pytest -n0 "tests/test_langgraph.py::test_a_real_toolexception_and_an_injected_error_are_handled_alike"` |
| The fake model's replies against real ones | Every field the fake model server sends is in a reply saved from a real call to OpenAI | `uv run pytest -n0 tests/test_fixtures_match_real.py` |
| The pages of this site | Every example and the quickstart run, and print what their pages show | `uv run pytest -n0 tests/test_docs_examples.py` |

The suite also requires at least 95% branch coverage of the product code, measured in every process a test starts, across Python 3.12 to 3.14.
Python 3.11 runs every test with no coverage tracer, as its `sys.settrace` can fail in a new thread while another thread is in an audit hook.
