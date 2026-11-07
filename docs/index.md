# Scrimmage

Scrimmage tests what your AI agent does when the things it depends on fail.

An agent asks a model what to do, calls tools, and asks again until it has an answer.
In a normal test every tool answers, so you never see what the agent does when one fails.
Scrimmage makes those failures happen on purpose, inside your agent's own Python process.
It needs no change to the agent's code, as long as the agent looks each tool up on its module when it calls it.
See [A function agent](examples/function-agent.md).
Then it checks the answer against rules you write.

For each call your agent makes through a gate, a test decides what it gets:

- **real**: the call runs as normal, if the test allows it;
- **mock**: a fixed answer, and the real call never happens;
- **fault**: a failure that looks like a real one, such as a timeout or an HTTP 429 from the model.

Scrimmage does not detect writes.
A gated call that writes, such as a listed refund tool, an MCP tool call or an httpx POST, never runs for real unless the test lists it.
Every gated call goes into a record on your machine, so you can see what the agent was given and what it did next.
[Safety](safety.md) lists what is gated, and [Limits](limits.md) lists what is not.

[Install](install.md){ .md-button .md-button--primary }
[Quickstart](quickstart.md){ .md-button }

## An example

To test an agent, you write two things:

- a Python function that takes the question as text and returns the agent's answer;
- a `scrimmage.toml` file, which names that function, its tools, and the tests.

The agent calls a real model, so you need an OpenAI API key.
To try Scrimmage without one, the repository's examples run on a fake model server; see [Examples](examples/index.md).
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
It leaves `refund_order` unmocked and does not list it as safe, so a call to it is refused:

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
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30", "October 30"]
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
  httpx is a widely used Python HTTP client, and the `openai` library sends its requests with it.
  httpx2 is a newer client from the Pydantic team, which the `mcp` library uses.
- Tools on MCP servers.
  MCP, the Model Context Protocol, is a standard way for an agent to call tools that run in a separate program; agent frameworks and coding assistants use it.
- OpenAI model calls, and gateways that speak the same format, with a spending limit for each test.
- Rules on the answer, and a pass rate for each row.

What it does not do yet is on the [Limits](limits.md) page.
Scrimmage is at an early stage, and has no published release yet.

## Where it comes from

Fault injection is an established practice.
Chaos engineering fails servers and networks on purpose, to find weak points before an outage does.
Service proxies such as Envoy, which many companies run in front of their services, can add delays and errors to requests for the same reason.

Others have applied the idea to AI agents.
These descriptions come from each project's own public pages, as of November 2026:

- [AgentChaos](https://github.com/floritange/AgentChaos), a research project, wraps the HTTP client inside an agent's process at run time and injects server errors, cut-off replies and corrupted tool calls, with no change to the agent's code.
- [AgentCheck](https://arxiv.org/abs/2607.11098), a 2026 paper and workbench, injects faults at an MCP server, records and replays tool calls, and scores runs with rules and a model judge.
- [ReliabilityBench](https://arxiv.org/abs/2601.06112), a 2026 benchmark, measures agents under timeouts, rate limits, partial replies and schema changes.
- [Owotogbe and others](https://arxiv.org/abs/2505.03096), a 2025 paper, propose chaos engineering for systems of several LLM agents.
- [agentbreak](https://github.com/mnvsk97/agentbreak) is a proxy that the agent's base URL points at; it injects latency, HTTP errors and broken replies into OpenAI, Anthropic and MCP calls.
- [aimock](https://github.com/CopilotKit/aimock), from CopilotKit, is a mock server for model APIs, MCP tools and more, also reached by changing the base URL.

Scrimmage reproduces this practice for a Python agent's own calls.
What it adds is in how it applies it:
it gates the agent's own Python tools by import path, inside the process, with no proxy;
it refuses a gated call that could write unless the test lists it;
and it judges each answer with rules you write, over repeated runs.

## Next

- [Install](install.md), then the [Quickstart](quickstart.md): a failing row, then a fix.
- [Examples](examples/index.md): scripts, LangGraph, MCP, web requests, judging and one model.
- [Safety](safety.md) and [Security](security.md): what a test can and cannot do to your systems and data.
- [Configuration](configuration.md) and [Commands](cli.md): the reference.
