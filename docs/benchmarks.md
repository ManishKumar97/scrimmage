# Benchmarks

!!! note "Numbers are coming"
    This page has no performance numbers yet.
    They come in a later release.
    This page will show each number with the machine it ran on and the command that reproduces it.
    Each number will also come with a budget per call that CI checks, so a slower change fails the build.

## What will be measured

| Measure | What it means |
|---|---|
| Overhead per gated tool call | The time Scrimmage adds to one call of a listed Python tool, for a mock, a fault and a real call |
| Overhead per gated web request | The same for a request through httpx or httpx2 |
| Overhead per gated MCP call | The same for an MCP tool call, over stdio and over streamable HTTP |
| Overhead per gated model call | The same for a model call, including the budget reservation |
| Startup cost | The time from `scrimmage-run` starting a program to the agent's first line of code, with the gates on |
| CLI time | The time `scrimmage test` spends on its own work: reading the file, starting runs, and writing the report |

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

The suite also requires at least 95% branch coverage of the product code, measured in every process a test starts, across Python 3.11 to 3.14.
