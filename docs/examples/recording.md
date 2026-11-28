# Recording what your service answers

You do not have to write a mock for every call by hand.
`scrimmage record` runs a test's baseline once, lets the tools under `real` answer for real, and writes what they answered as mocks in a copy of your file.
Then `scrimmage test` on that copy runs without the service.

This page starts from the [web request](web-request.md) example: a tool that asks the orders service over the web.
Read [Recording replies](../recording.md) for every case this page leaves out.

Libraries this example needs: `openai` and `httpx`.
This page was checked with openai 3.46 and httpx 0.28.
The orders service is a stand-in on `localhost:8765`.
The repository's tests run this page on a scripted model, and the release check runs it once on `gpt-4o-mini`.
Costs and the model's words differ from the page.

## The files

The tools are the web request example's, with the orders service at a local address:

```python title="shop/tools.py"
"""The bot's tools. get_order asks the orders service over the web."""

import httpx

ORDERS = "http://localhost:8765"


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

`shop/agent.py` is the function agent's, unchanged.

The file has a test and no mock.
`get_order` is under `real`, so it runs, and the request inside it goes to the service:

```toml title="scrimmage.toml"
# The order bot's tests. get_order is under real, so a recording asks the orders service.
# The test has no mock: scrimmage record writes one, into a copy of this file.
# Read the copy before you commit it.

[agent]
function = "shop.agent:answer"
tools    = ["shop.tools:get_order", "shop.tools:refund_order"]
real     = ["tool:get_order"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]
```

## Record

```bash
scrimmage record
```

```text title="Output"
scrimmage record: 1 test, 1 run; records in /home/me/shop/scrimmage-runs/20261126T083602Z-4239e5
order-status  run-2766f5f6c41d  answered  wrote 1
wrote 1 mock to scrimmage.recorded.toml; scrimmage.toml is unchanged
recorded 1 of 1 calls that needed a mock
warning: the mocks hold replies from real systems, which can hold customer data; check them before you commit them
model calls cost $0.0000 of a budget of $5
next: diff scrimmage.toml scrimmage.recorded.toml, then scrimmage test scrimmage.recorded.toml
--- scrimmage.toml
+++ scrimmage.recorded.toml
@@ -1,6 +1,7 @@
 # The order bot's tests. get_order is under real, so a recording asks the orders service.
 # The test has no mock: scrimmage record writes one, into a copy of this file.
 # Read the copy before you commit it.
+# Written by scrimmage record from scrimmage.toml on 2026-11-26. Review it, then rename it to scrimmage.toml.
 
 [agent]
 function = "shop.agent:answer"
@@ -11,3 +12,12 @@
 name     = "order-status"
 question = "Where is order 1234?"
 expect   = ["shipped", ["Oct 30", "October 30"]]
+
+# scrimmage record, 2026-11-26, run-2766f5f6c41d: http:localhost GET /orders/1234, 1 call.
+[[test.mock]]
+call     = "http:localhost"
+method   = "GET"
+path     = "/orders/1234"
+recorded = true
+status   = 200
+json     = {status = "shipped", date = "Oct 30"}
```

The model call is real and costs money, within the budget, as in `scrimmage test`.
It is never recorded.
A scripted model reply would replace what the test exists to see, so the model answers live every time.

`scrimmage.toml` is unchanged.
The copy, `scrimmage.recorded.toml`, holds the test and one new mock:

```toml title="scrimmage.recorded.toml"
# The order bot's tests. get_order is under real, so a recording asks the orders service.
# The test has no mock: scrimmage record writes one, into a copy of this file.
# Read the copy before you commit it.
# Written by scrimmage record from scrimmage.toml on 2026-11-26. Review it, then rename it to scrimmage.toml.

[agent]
function = "shop.agent:answer"
tools    = ["shop.tools:get_order", "shop.tools:refund_order"]
real     = ["tool:get_order"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

# scrimmage record, 2026-11-26, run-2766f5f6c41d: http:localhost GET /orders/1234, 1 call.
[[test.mock]]
call     = "http:localhost"
method   = "GET"
path     = "/orders/1234"
recorded = true
status   = 200
json     = {status = "shipped", date = "Oct 30"}
```

Each mock has a comment that says where it came from.
`recorded = true` marks a mock that record wrote.
The mock is on the web request and not on `get_order`, so `get_order`'s own code still runs and only the service is replaced.

Read a recording before you commit it.
It holds what your real service answered, which can be customer data.

## Run it without the service

```bash
scrimmage test scrimmage.recorded.toml
```

```text title="Output"
scrimmage test: 1 test, 1 row, 3 runs; records in /home/me/shop/scrimmage-runs/20261126T083710Z-c4ace8
test          row       fault  runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -      3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0001 of a budget of $5
```

The service was stopped for this run, and the test passed.
The mock answered the request, and the model, still real, said what the mock told it.

More: [Recording replies](../recording.md), [Web requests](../web-requests.md).
