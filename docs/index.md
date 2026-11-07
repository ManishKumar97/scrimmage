# Scrimmage

Scrimmage tests what your AI agent does when the things it depends on fail.

An agent asks a model what to do, calls tools, and asks again until it has an answer.
In a normal test every tool answers, so you never see what the agent does when one fails.
Scrimmage makes those failures happen on purpose, inside your agent's own Python process, with no change to its code.
Then it checks the answer against rules you write.

For each call your agent makes, a test decides what it gets:

- **real**: the call runs as normal, if the test allows it;
- **mock**: a fixed answer, and the real call never happens;
- **fault**: a failure that looks like a real one, such as a timeout or an HTTP 429 from the model.

A call that writes, such as a refund, never runs for real unless the test lists it.
Every call goes into a record on your machine, so you can see what the agent was given and what it did next.

[Install](install.md){ .md-button .md-button--primary }
[Quickstart](quickstart.md){ .md-button }

## An example

The bot has a tool that looks up an order:

```python
# shop/tools.py
def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    ...
```

The test mocks the tool, then runs again with the tool failing:

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
- Its own Python tools, the web requests inside them through httpx, and tools on MCP servers.
- OpenAI model calls, and gateways that speak the same format, with a spending limit for each test.
- Rules on the answer, and a pass rate for each row.

What it does not do yet is on the [Limits](limits.md) page.
Scrimmage is at an early stage, and has no published release yet.

## Where it comes from

Fault injection is an established practice.
Chaos engineering fails servers and networks in production on purpose, to find weak points before an outage does.
Service proxies such as Envoy, which many companies run in front of their services, can add delays and errors to requests for the same reason.
Others have applied the idea to AI agents before.

Scrimmage reproduces that practice for an agent's own calls, and extends it in a few ways:
it works inside the agent's Python process with no code change, it refuses writes that a test did not allow, and it judges each answer with rules you write.

## Next

- [Install](install.md), then the [Quickstart](quickstart.md): a failing row, then a fix.
- [Examples](examples/index.md): scripts, LangGraph, MCP, web requests, judging and one model.
- [Safety](safety.md) and [Security](security.md): what a test can and cannot do to your systems and data.
- [Configuration](configuration.md) and [Commands](cli.md): the reference.
