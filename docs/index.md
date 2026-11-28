# Scrimmage

Scrimmage tests what your AI agent does when the things it depends on fail.

An agent asks a model what to do, calls tools, and asks again until it has an answer.
In a normal test every tool answers, so you never see what the agent does when one fails.
Scrimmage makes those failures happen on purpose, inside your agent's own Python process.
It needs no change to the agent's code, as long as the agent looks each tool up on its module when it calls it.
See [A function agent](examples/function-agent.md).
Then it checks the answer against rules you write.

For each call your agent makes through a [gate](concepts.md#gate), a test decides what it gets:

- **real**: the call runs as normal, if the test allows it;
- **mock**: a fixed answer, and the real call never happens;
- **fault**: a failure that looks like a real one, such as a timeout or an HTTP 429 from the model.

Scrimmage does not detect writes.
It decides by the kind of call.
A [gated](concepts.md#gate) call that can write, such as a refund tool listed under `tools`, an MCP tool call, or an httpx request that is not a `GET` or `HEAD`, never runs for real unless the test mocks it or lists it under `real`.
A web `GET` or `HEAD` runs for real, even when the service changes data on it, so mock a `GET` that writes.
Every gated call goes into a record on your machine, so you can see what the agent was given and what it did next.
[Safety](safety.md) lists what is gated, and [Limits](limits.md) lists what is not.

## Your data stays on your machine

Scrimmage runs on your machine, in your CI or in your cluster.
Scrimmage has no telemetry and no hosted service, and it sends nothing to its authors.
It makes three kinds of connection, and no others: to its own control server, to the service address your `scrimmage.toml` names for a web service, and to GitHub when you run `scrimmage prices update`, which downloads one public file of model prices.
That command sends a GET request, so GitHub sees your IP address and the `User-Agent` of Python's urllib, and nothing about your agent, your tests or your data.
If `HTTPS_PROXY` is set, the request goes to that proxy, which sees the connection, and not straight to GitHub.
No test run makes that connection, and nothing else in Scrimmage does.
Where the control server listens, and what your agent's own calls do, are on [Security](security.md#your-data-stays-on-your-machine).
Each of these statements is checked by a test; see [Security](security.md).

## Install

Scrimmage needs Python 3.11 or later.
It is not released on PyPI yet, so you install it from a clone of the repository.
A plain `pip install scrimmage` takes an empty placeholder, so do not use it.

```bash
git clone https://github.com/ManishKumar97/scrimmage
cd scrimmage
python -m venv .venv
. .venv/bin/activate
pip install ./packages/scrimmage
```

[Install](install.md) has uv, a wheel and the extras.
Then follow the [5-minute quickstart](quickstart.md): a failing row, then a fix.

[Install](install.md){ .md-button .md-button--primary }
[Quickstart](quickstart.md){ .md-button }

## An example

To test an agent, you write two things:

- a Python function that takes the question as text and returns the agent's answer;
- a `scrimmage.toml` file, which names that function, its tools, and the tests.

The agent calls a real model, so you need an OpenAI or an Anthropic API key.
For an agent on Anthropic's library, see [An agent that uses the anthropic library](model-faults.md#an-agent-that-uses-the-anthropic-library) and the [worked example](examples/anthropic-agent.md).
Scrimmage is not on PyPI yet, so you install it from a clone of the repository; see [Install](install.md).

The bot has two tools, one that reads and one that writes:

```python
# shop/tools.py
def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    ...


def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    ...
```

The test mocks `get_order`, then runs again with it failing.
It leaves `refund_order` unmocked and does not list it as safe, so a gated call to it is refused:

```toml
# scrimmage.toml
[agent]
function = "shop.agent:answer"
tools    = ["shop.tools:get_order", "shop.tools:refund_order"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call   = "tool:get_order"
do     = "error"
raises = "ConnectionError"
expect = [["unable", "not able", "wasn't able", "was not able", "can't", "cannot", "couldn't", "could not", "don't have access", "do not have access", "i don't have", "i don't know", "timed out", "ran into", "sorry", "unfortunately", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "issue retrieving", "issue with retrieving", "timeout issue", "i'm experiencing", "i am experiencing", "i'm currently experiencing"]]
forbid = ["shipped", "ships", "will ship", "ship soon", "ship today", "ship tomorrow", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "april", "june", "july", "august", "september", "december"]
```

`scrimmage test` runs each row three times, each time as a new program, and reports:

```text
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T194000Z-1a2b3c
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $5
```

With the tool failing, the bot said it could not look the order up, and gave no date, so the row passes.
A bot that hid the error and answered with a date anyway would fail this row.
The whole project is on the [function agent](examples/function-agent.md) page.

## What it covers today

- An agent that is a Python function, with its own loop or on LangGraph, or a Python script and the child processes it starts.
  LangGraph is LangChain's library for agents built as a graph of steps; teams use it to run tool-calling agents.
- Its own Python tools, and the web requests inside them sent with httpx or httpx2.
  httpx is a widely used Python HTTP client.
  httpx2 is its successor from the Pydantic team; the `openai` and `mcp` libraries send their requests with it.
- Tools on MCP servers.
  MCP, the Model Context Protocol, is a standard way for an agent to call tools that run in a separate program; agent frameworks and coding assistants use it.
- OpenAI and Anthropic model calls, and gateways that speak either format, with a spending limit for each test.
- Rules on the answer, and a pass rate for each row.

What it does not do yet is on the [Limits](limits.md) page.
Scrimmage is at an early stage, and has no published release yet.

## Where it comes from

Fault injection is an established practice.
Chaos engineering fails servers and networks on purpose, to find weak points before an outage does.
Service proxies such as Envoy, which many companies run in front of their services, can add delays and errors to requests for the same reason.

Others have applied the idea to AI agents.
These descriptions come from each project's own public pages, as of November 2026:

- [AgentChaos](https://arxiv.org/abs/2608.06790), a 2026 paper, injects server errors, truncated replies and corrupted content or tool-call fields into LLM API responses at run time, at the HTTP layer every agent shares, with no change to source code.
- [AgentCheck](https://arxiv.org/abs/2607.11098), a 2026 paper and workbench, injects faults at an MCP server, records and replays tool calls, and scores runs with rules and a model judge.
- [ReliabilityBench](https://arxiv.org/abs/2601.06112), a 2026 benchmark, measures agents under timeouts, rate limits, partial replies and schema changes.
- [Owotogbe and others](https://arxiv.org/abs/2505.03096), a 2025 paper, propose chaos engineering for systems of several LLM agents.
- [agentbreak](https://github.com/mnvsk97/agentbreak) is a proxy that the agent's base URL points at; it injects latency, HTTP errors and broken replies into OpenAI, Anthropic and MCP calls.
- [aimock](https://github.com/CopilotKit/aimock), from CopilotKit, is a mock server for model APIs, MCP tools and more, also reached by changing the base URL.

Scrimmage reproduces this practice for a Python agent's own calls.
What it adds is in how it applies it:
it gates the agent's own Python tools by import path, inside the process, with no proxy;
it refuses a gated tool call, MCP tool call or web write unless the test lists it;
and it judges each answer with rules you write, over repeated runs.

## Next

- [Install](install.md), then the [Quickstart](quickstart.md): a failing row, then a fix.
- [Judging an answer](judging.md), [Failing the model](model-faults.md) and [Testing your guardrails](guardrails.md): the main use cases.
- [Examples](examples/index.md): a function, [an agent written with the anthropic library](examples/anthropic-agent.md), [an agent behind a model gateway](examples/gateway.md), [CI](examples/ci.md), [recording](examples/recording.md), [a budget](examples/budget.md), a script, a web service, LangGraph, MCP, web requests, an unknown tool, judging and one model.
- [Safety](safety.md) and [Security](security.md): what a test can and cannot do to your systems and data.
- [Configuration](configuration.md), [Commands](cli.md) and [JSON output](json-output.md): the reference.
- [Source on GitHub](https://github.com/ManishKumar97/scrimmage), the [License](license.md) (Apache-2.0) and [Security](security.md).
