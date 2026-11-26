# A function agent

The smallest complete project: an agent that is one Python function with its own loop, two tools, and one test.
Copy the three files into an empty folder to start your own.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.

## The files

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
"""An order bot: a loop over the openai library and two tools."""

import json

import openai

from shop import tools

SPECS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": getattr(tools, name).__doc__,
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
    for name in ("get_order", "refund_order")
]


def run_tool(name: str, arguments: str) -> str:
    try:
        return str(getattr(tools, name)(**json.loads(arguments)))
    except Exception as exc:  # tell the model what went wrong
        return f"error: {type(exc).__name__}: {exc}"


def answer(question: str) -> str:
    client = openai.OpenAI()
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=SPECS)
        message = reply.choices[0].message
        if not message.tool_calls:
            return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            result = run_tool(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "I could not finish."
```

The bot looks a tool up on its module, `tools.get_order`, at the moment it calls it.
That is how Scrimmage reaches it: it swaps each listed tool for a gate when your code imports the module.

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
forbid = ["shipped", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday"]
```

`refund_order` is listed but neither mocked nor under `real`, so if the model asks for a refund, the call is refused and the run stops.
The fault row raises `ConnectionError` inside `get_order`, so the bot's own `except` runs.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T194000Z-1a2b3c
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $5
```

In the fault row the bot passed the error to the model, and the model said it could not look the order up.

More: [Quickstart](../quickstart.md), [Configuration](../configuration.md), [Safety](../safety.md).
