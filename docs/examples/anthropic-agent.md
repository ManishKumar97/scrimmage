# An agent written with the anthropic library

The [function agent](function-agent.md), written for Claude.
It is one Python function with its own loop over `anthropic.Anthropic().messages.create`, a `tool_use` block for each tool call, and a `tool_result` block for each answer.
Scrimmage reads the same `scrimmage.toml`.
A model fault is an Anthropic error, and a call is priced from the reply's own `usage`.

Libraries this example needs: `anthropic`.
This page was checked with anthropic 2.12.
The repository's tests run it on a scripted model, and the release check runs it once on `claude-haiku-5-5`.
Costs and the model's words differ from the page.

## The files

The tools are the function agent's, unchanged:

```python title="shop/tools.py"
"""The bot's tools. In a test, Scrimmage mocks them, fails them or refuses them."""


def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    raise RuntimeError("the orders system is not reachable from this machine")


def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    raise RuntimeError("a real refund must never run in a test")
```

```python title="shop/agent.py"
"""An order bot: a loop over the anthropic library and two tools."""

import anthropic

from shop import tools

SPECS = [
    {
        "name": name,
        "description": getattr(tools, name).__doc__,
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    }
    for name in ("get_order", "refund_order")
]


def run_tool(name: str, arguments: dict) -> dict:
    try:
        return {"content": str(getattr(tools, name)(**arguments))}
    except Exception as exc:  # tell the model what went wrong
        return {"content": f"error: {type(exc).__name__}: {exc}", "is_error": True}


def answer(question: str) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]
    try:
        for _ in range(8):
            reply = client.messages.create(
                model="claude-haiku-5-5", max_tokens=512, messages=messages, tools=SPECS
            )
            calls = [block for block in reply.content if block.type == "tool_use"]
            if not calls:
                return "".join(block.text for block in reply.content if block.type == "text")
            content = [block.model_dump(exclude_none=True) for block in reply.content]
            messages.append({"role": "assistant", "content": content})
            results = [
                {"type": "tool_result", "tool_use_id": call.id, **run_tool(call.name, call.input)}
                for call in calls
            ]
            messages.append({"role": "user", "content": results})
    except anthropic.APIStatusError as error:  # the library has already retried it twice
        return f"the model failed: {type(error).__name__}"
    return "I could not finish."
```

The bot looks a tool up on its module at the moment it calls it, as the function agent does, so Scrimmage can swap each listed tool for a gate.
A failed tool goes back to the model as a `tool_result` with `is_error` set, which is how the Messages API wants a failure told.

```toml title="scrimmage.toml"
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

[[test.fault]]
call   = "model"
do     = "status 529"
when   = "first 3"
expect = ["OverloadedError"]
```

The first fault row raises `ConnectionError` inside `get_order`, so the bot's own `except` runs and the model is told.
The second row fails the model call itself with HTTP 529, Anthropic's "overloaded" error.
The `anthropic` library retries a 529 twice by itself, so `first 3` fails the first try and both retries, and the bot sees the error only then.
A row that said `first 1` would show a model that recovered, and the bot would never notice.

Scrimmage knows Anthropic's own address, `api.anthropic.com`, so this file names no gateway.
Set `ANTHROPIC_API_KEY` as you would to run the bot.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 3 rows, 9 runs; records in /home/me/shop/scrimmage-runs/20261126T065940Z-c6d80d
test          row       fault                     runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                         3     -      3     3/3     100%  44-100%  1.0    0.00022  pass
order-status  fault 1   tool:get_order error      3     3      3     3/3     100%  44-100%  1.0    0.00022  pass
order-status  fault 2   model status 529 first 3  3     3      3     3/3     100%  44-100%  0.0    0.00000  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0004 of a budget of $5
```

In the first fault row the bot passed the error to the model, and the model said it could not look the order up.
In the second the model never answered: the bot caught `OverloadedError` and said so.
A faulted call is never sent, so the second row cost nothing.

More: [Model faults](../model-faults.md), [Configuration](../configuration.md).
