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
| Tool call, sync | 0.2 | 62.5 | 62.3 (61.4 to 62.8) | 0.2 | 138.9 | 138.6 (113.3 to 150.3) |
| Tool call, async | 0.3 | 63.2 | 62.9 (62.3 to 64.4) | 0.3 | 147.4 | 147.1 (141.5 to 152.1) |
| Web request through httpx, sync | 378.1 | 790.3 | 404.4 (355.2 to 429.6) | 559.9 | 1202.4 | 644.6 (620.5 to 1251.2) |
| Web request through httpx, async | 651.7 | 1198.7 | 552.6 (507.0 to 565.0) | 893.5 | 1733.1 | 839.6 (767.4 to 2076.8) |
| Model call, sync, budget on | 1269.7 | 3037.1 | 1769.1 (1751.7 to 1856.8) | 1425.2 | 4275.8 | 2803.4 (2464.0 to 2924.5) |
| Model call, async, budget on | 1585.7 | 3788.1 | 2202.4 (2097.7 to 2272.3) | 2211.3 | 6014.8 | 3961.1 (3458.4 to 4524.8) |
| Model call, sync, budget off | 1269.7 | 1838.7 | 574.0 (555.4 to 583.1) | 1425.2 | 2557.3 | 1134.4 (864.0 to 1288.5) |
| Model call, async, budget off | 1585.7 | 2254.2 | 668.5 (611.4 to 728.4) | 2211.3 | 3169.2 | 951.5 (688.4 to 1222.6) |
| MCP tool call, stdio, async | 1302.6 | 1593.9 | 300.2 (248.3 to 332.1) | 1662.0 | 2331.5 | 736.3 (594.9 to 750.2) |

A mocked call makes no real call, so it has no ungated row to compare with.
A mocked tool call takes 66.7 µs at the median, and a mocked web request 185.4 µs.

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
| Plain Python | 9.1 (8.8 to 9.4) | 0 | 11.2 (11.1 to 11.4) | 0 |
| Hooks loaded, root process | 93.3 (92.5 to 93.7) | 84.1 | 214.9 (212.7 to 218.7) | 203.8 |
| Hooks loaded, child process | 92.3 (92.1 to 93.5) | 83.3 | 108.9 (108.5 to 110.0) | 97.8 |
| Through `scrimmage-run`, the real path | 115.4 (113.5 to 116.2) | 106.4 | 237.1 (232.9 to 237.4) | 225.7 |

A run's root process waits at exit for its last records to be sent, which costs about 100 ms.
A child process sends each record as it is made, so it does not wait.

## CLI

Wall time in milliseconds, with the median of 5 repeats of 5 commands each.

| Command | Wall ms (spread) | Added to plain Python |
|---|---|---|
| Plain Python | 11.3 (11.1 to 12.2) | 0 |
| Import of `scrimmage_core.cli` | 100.1 (99.4 to 100.2) | 88.3 |
| `scrimmage --help` | 101.9 (101.5 to 102.9) | 90.6 |
| `scrimmage check` on the example | 101.9 (101.0 to 105.5) | 90.8 |

## Runner overhead per run

For an agent that does nothing, started one at a time, in milliseconds.
The cost per run comes from the slope between a test of 1 run and a test of 11 runs.

| Measure | ms (spread) |
|---|---|
| Whole cost of one run | 267.5 (267.2 to 267.9) |
| The same agent under plain Python | 11.8 (11.6 to 12.0) |
| Added per run by the runner and hooks | 255.5 (255.4 to 256.3) |
| Fixed cost of one `scrimmage test` | 556.5 (554.7 to 560.6) |

## Budgets

The suite's slow tier has a test, `tests/test_benchmark_budgets.py`, that fails when a cost goes over its ceiling.
The ceilings are set far above today's quiet numbers, and above the worst seen on a machine under heavy load.
That is 8 times for a tool call, 19.8 times for a sync web request, 14.5 times for an async one, 20 times for an MCP call, 11.3 times for a sync model call, 9.1 times for an async one, and 8.8 times for the CLI.
So they do not fail because the machine is busy, and they catch only a large slowdown.
Of the per-call ceilings, only the tool call's catches one added round trip to the runner.
A web request needs about 7.6 ms added to fail, and a model call about 18 ms.

The same test file has two checks that use no clock.
One counts the requests to the runner that a gated call waits on: none for a tool, a web request or an MCP call, one for a model call with a budget, and none for a model call without one.
The other imports `scrimmage_core.cli` in a new process and checks that `openai` and `httpx` are not loaded.

Nothing enforces a smaller slowdown.

| Measure | Ceiling |
|---|---|
| Tool call, added | 500 µs |
| Web request, added | 8 ms |
| Model call with a budget, added | 20 ms |
| MCP tool call, added | 6 ms |
| Startup, added to the first line | 800 ms |
| Startup, added to process exit | 1500 ms |
| `scrimmage check`, added | 800 ms |
| `scrimmage --help`, added | 800 ms |
| Import of `scrimmage_core.cli`, added | 800 ms |
| One run, added | 2000 ms |

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
