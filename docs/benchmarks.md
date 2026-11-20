# Benchmarks

These are real measurements, all from one machine.
Treat them as a guide to the size of each cost, not as a promise about yours.

**Machine:** a Microsoft Azure `Standard_D4as_v4` virtual machine, x86_64, with 4 vCPUs and Linux, running Python 3.11.17.

**Where the numbers come from:**

- Every number on this page comes from one run of `tests/benchmark.py` with the default 5 rounds.
- Commit: `2b011b8`.
- Date: 2026-11-20, 14:15 UTC.
- The run had the machine to itself, with no other work running.
- The load average was 0.76 when the run was staged and 1.34 at its start, from a `uv sync` that had just finished.
- It was 1.27 after the run.
- Libraries, as pinned in `uv.lock` at that commit: httpx 0.28.1, httpx2 2.16.0, openai 3.46.0, mcp 2.3.0, uvicorn 0.54.0 and langchain-core 1.8.4.

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
| Tool call, sync | 0.2 | 52.3 | 52.1 (51.1 to 52.8) | 0.2 | 119.6 | 119.4 (108.9 to 129.4) |
| Tool call, async | 0.3 | 52.2 | 51.9 (51.5 to 52.1) | 0.3 | 111.9 | 111.6 (108.3 to 118.2) |
| Tool call, args miss, on a tool under `real` | 0.2 | 150.3 | 150.1 (148.7 to 152.7) | 0.3 | 382.2 | 381.9 (330.9 to 443.3) |
| Web request through httpx, sync | 387.8 | 746.9 | 356.9 (266.2 to 385.7) | 564.4 | 1311.3 | 732.1 (631.2 to 989.6) |
| Web request through httpx, async | 694.1 | 1136.7 | 444.4 (431.0 to 460.0) | 884.8 | 1673.3 | 806.4 (705.1 to 833.4) |
| Model call, sync, budget on | 1308.8 | 3038.3 | 1733.6 (1686.8 to 1829.1) | 1517.0 | 4333.7 | 2678.8 (2158.9 to 3047.2) |
| Model call, async, budget on | 1628.0 | 3842.0 | 2214.0 (2173.9 to 2301.7) | 2326.8 | 6284.0 | 4565.6 (3339.1 to 4624.2) |
| Model call, sync, budget off | 1308.8 | 1794.1 | 485.2 (467.0 to 491.9) | 1517.0 | 2457.1 | 775.9 (368.5 to 1386.9) |
| Model call, async, budget off | 1628.0 | 2240.3 | 611.8 (588.7 to 648.4) | 2326.8 | 3674.2 | 1720.1 (894.9 to 2568.7) |
| MCP tool call, stdio, async | 1355.5 | 1688.3 | 341.0 (315.1 to 392.9) | 1717.8 | 2600.5 | 1082.8 (376.5 to 1184.3) |

A mocked call makes no real call, so it has no ungated row to compare with.
A mocked tool call takes 50.4 µs at the median, and a mocked web request 208.5 µs.
A call that misses a mock with `args`, on a tool under `real` by name, runs for real and adds about 150 µs, as it looks for the nearest mock and keeps the evidence.

A gated model call with a budget on makes one extra signed request to the runner, to reserve the call's money.
That is the largest part of the time it adds.

A real provider's response time was not measured here.
If a model call takes 200 ms, the 1.7 ms a sync call adds with a budget on is 0.9 percent, and the 2.2 ms an async call adds is 1.1 percent.

The MCP library has no sync client, so MCP has only an async row.
The MCP rows use the stdio transport.

## Change since the last page

The last page came from runs on 2026-11-09 and 2026-11-11.
A bisect on 2026-11-20 explained the rows that moved.

- Model calls grew after PR #46 on 11 November.
  A redaction pre-check matched `max_tokens` in every body, so its full pattern ran on every request and reply.
  PR #162 fixed it, and the redactor is now also faster than before on nested text.
  The model rows are back near the old page: 1733.6 µs against 1645.5 µs for a sync call with a budget on, and 2214.0 against 2071.2 for an async one.
- A mocked tool call grew after PR #105, which resolved the return type on every call.
  PR #161 caches it.
  It now takes 50.4 µs, against 48.0 µs on the old page.
- The 9 November tool rows measured a shared plan that later gained an args-miss mock.
  PR #161 measures that row in its own plan, so the old and new tool rows are not the same measure.
- What is left is on a real tool call.
  It adds about 52 µs now, against 43 µs on 9 November, which is 9 µs more.
  The bisect put that on miss recording (PR #42) and the scan for secret literals.
- Startup grew about 15 ms, for example 68.2 to 86.2 ms to the first line for a root process, and 45.2 to 61.0 ms for the import of `scrimmage_core.cli`.
  That comes from the redactor compiling more patterns and from config, and it is tracked.

The MCP call went from 257.3 to 341.0 µs at the median, which is 33 percent.
That change is not explained.
The bisect did not cover it, and its spread in this run, 315.1 to 392.9, is wide.
The other first-table rows moved by less than 20 percent, except the tool rows, which are explained above.

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
| Plain Python | 9.2 (9.1 to 9.5) | 0 | 11.3 (11.2 to 11.5) | 0 |
| Hooks loaded, root process | 95.7 (95.0 to 96.8) | 86.2 | 112.9 (112.3 to 114.8) | 101.5 |
| Hooks loaded, child process | 92.6 (91.9 to 94.4) | 83.3 | 107.0 (106.3 to 108.2) | 95.7 |
| Through `scrimmage-run`, the real path | 118.4 (117.1 to 120.6) | 109.2 | 135.9 (134.3 to 136.2) | 124.6 |

A run's root process wakes the thread that sends records when it exits, so its last records go at once and it does not wait for that thread's 100 ms window.
A child process sends each record as it is made, so it has none left to send.

## CLI

Wall time in milliseconds, with the median of 5 repeats of 5 commands each.

| Command | Wall ms (spread) | Added to plain Python |
|---|---|---|
| Plain Python | 11.5 (11.2 to 11.7) | 0 |
| Import of `scrimmage_core.cli` | 72.6 (71.9 to 72.7) | 61.0 |
| `scrimmage --help` | 76.8 (76.7 to 78.0) | 65.6 |
| `scrimmage check` on the example | 76.5 (75.7 to 77.0) | 65.0 |

## Runner overhead per run

For an agent that does nothing, started one at a time, in milliseconds.
The cost per run comes from the slope between a test of 1 run and a test of 11 runs.

| Measure | ms (spread) |
|---|---|
| Whole cost of one run | 168.2 (166.9 to 169.0) |
| The same agent under plain Python | 12.3 (12.1 to 13.8) |
| Added per run by the runner and hooks | 155.5 (154.8 to 156.2) |
| Fixed cost of one `scrimmage test` | 584.7 (584.4 to 593.5) |

## Budgets

The suite's slow tier has a test, `tests/test_benchmark_budgets.py`, that fails when a cost goes over its ceiling.
The ceilings are set far above today's quiet numbers, and above the worst seen on a machine under heavy load.
That is 9.6 times for a tool call, 16.8 times for a sync web request, 16.9 times for an async one, 17.6 times for an MCP call, 11.5 times for a sync model call, 10.4 times for an async one, and 8.0 to 9.1 times for the CLI.
So they do not fail because the machine is busy, and they catch only a large slowdown.
Of the per-call ceilings, only the tool call's catches one added round trip to the runner.
A sync web request needs about 5.6 ms added to fail, and an async one about 7.1 ms.
A sync model call needs about 18.3 ms added to fail, and an async one about 20.8 ms.
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
| Web request, added | 6 ms sync, 7.5 ms async |
| Model call with a budget, added | 20 ms sync, 23 ms async |
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
