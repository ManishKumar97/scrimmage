# A web request inside a tool

Many tools are a thin wrapper around a web request to another service.
Scrimmage can let the tool's own code run, and mock or fail the request inside it instead.
Then the tool's parsing and error handling are part of the test.

Scrimmage gates requests sent with httpx, a common Python HTTP client, and httpx2.
The `openai` library uses httpx too.

Libraries this example needs: `openai` and `httpx`.
This page was checked with openai 3.46 and httpx 0.28.

## The files

Start from the three files of the [function agent](function-agent.md).
Replace the tools with ones that call the orders service over the web:

```python title="shop/tools.py"
"""The bot's tools. get_order asks the orders service over the web."""

import httpx

ORDERS = "https://orders.acme.internal"


def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    reply = httpx.get(f"{ORDERS}/orders/{order_id}", timeout=5)
    reply.raise_for_status()
    order = reply.json()
    return f"{order['status']}, {order['date']}"


def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    reply = httpx.post(f"{ORDERS}/orders/{order_id}/refund", timeout=5)
    reply.raise_for_status()
    return reply.text
```

```toml title="scrimmage.toml"
[agent]
function = "shop.agent:answer"
tools    = ["shop.tools:get_order", "shop.tools:refund_order"]
real     = ["tool:get_order"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call   = "http:orders.acme.internal"
method = "GET"
path   = "/orders/1234"
json   = {status = "shipped", date = "Oct 30"}

[[test.fault]]
call   = "http:orders.acme.internal"
do     = "status 503"
expect = [["unable", "not able", "wasn't able", "was not able", "can't", "cannot", "couldn't", "could not", "don't have access", "do not have access", "i don't have", "i don't know", "timed out", "ran into", "sorry", "unfortunately", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "issue retrieving", "issue with retrieving", "timeout issue", "i'm experiencing", "i am experiencing", "i'm currently experiencing"]]
forbid = ["shipped", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday"]
```

`real` lets `get_order` itself run.
The request inside it is answered by the mock, so the orders service is never called.
The fault answers the same request with HTTP 503, "service unavailable", so `raise_for_status` raises and the bot's own `except` runs.

The label of a web request is `http:` and the host.
A `GET` or `HEAD` that is not mocked runs for real, because it reads.
Any other method is refused unless `real` lists it, so the `POST` inside `refund_order` could never reach the service from this test.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T200000Z-9d0e1f
test          row       fault                           runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                               3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   http:orders.a...nal status 503  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
full name: order-status fault 1: http:orders.acme.internal status 503
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $5
```

A long fault name is cut in the table, and its full name is printed under it.
In the fault row, `get_order` raised on the 503, the bot passed the error to the model, and the model said it could not look the order up.
Each run's record has one line for the tool and one for the request inside it.
A request's line holds its method and path, never its headers, cookies or query string.

More: [Web requests](../web-requests.md), which covers paths, methods, redirects and what is recorded.
