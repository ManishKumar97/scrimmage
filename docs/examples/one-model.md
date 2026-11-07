# A fault on one model

An agent often calls more than one model: a large one to answer, and a small one as a fallback, a router or a guardrail.
A fault with `model` fails calls to one model by name and leaves the others alone.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.

## The files

Start from the three files of the [function agent](function-agent.md).
Make the bot answer with a larger model:

```diff title="shop/agent.py"
-        reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=SPECS)
+        reply = client.chat.completions.create(model="gpt-4.1", messages=messages, tools=SPECS)
```

Then test what happens when that model is busy:

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
call   = "model"
model  = "gpt-4.1"
do     = "status 429"
expect = ["shipped", ["Oct 30", "October 30"]]
```

`model` is the whole name your code sends, compared exactly.
The fault fires on every call to `gpt-4.1`, retries included, so the outage never ends within a run.
A fault that fires on every call must have its own rules; here they are the same as the test's, because the bot should still answer.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T193901Z-287275
test          row       fault                     runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                         3     -      3     3/3     100%  44-100%  1.0    0.00125  pass
order-status  fault 1   model gpt-4.1 status 429  3     3      0     0/3     0%    0-56%    0.0    0.00000  fail
range: a 95% Wilson interval, from these runs alone
fail: order-status fault 1: 0/3 passed, pass_rate is 1
  3 runs: crashed: /home/me/shop/scrimmage-runs/20261107T193901Z-287275/run-67a0277e8e12.log, /home/me/shop/scrimmage-runs/20261107T193901Z-287275/run-16ec9c0d2384.log, /home/me/shop/scrimmage-runs/20261107T193901Z-287275/run-96d374106084.log
model calls cost $0.0013 of a budget of $5
note: order-status fault 1: 3 crashed
```

The fault row fails: the `openai` library retried twice, then raised `RateLimitError`, and the bot crashed.

## Add a fallback

```diff title="shop/agent.py"
-        reply = client.chat.completions.create(model="gpt-4.1", messages=messages, tools=SPECS)
+        try:
+            reply = client.chat.completions.create(
+                model="gpt-4.1", messages=messages, tools=SPECS
+            )
+        except openai.RateLimitError:  # the large model is busy: use the small one
+            reply = client.chat.completions.create(
+                model="gpt-4o-mini", messages=messages, tools=SPECS
+            )
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T193922Z-74ff14
test          row       fault                     runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                         3     -      3     3/3     100%  44-100%  1.0    0.00125  pass
order-status  fault 1   model gpt-4.1 status 429  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0013 of a budget of $5
```

Calls to `gpt-4o-mini` ran for real, so the fallback answered.
A fault on the bare `model` label would have hit the fallback too, and the row would test nothing about it.

A name is compared whole: `gpt-4o-mini` does not name `gpt-4o-mini-2024-07-18`.
If no call sends the name, the fault never fires, and the test stops with exit code 2 and lists the names the calls used.

More: [Failing one model](../model-faults.md#failing-one-model), and its limits in [Limits](../limits.md).
