# An agent behind a model gateway

Many teams send every model call through a gateway, such as a LiteLLM proxy or OpenRouter.
The agent talks to the gateway in OpenAI's format, and the gateway talks to the provider.
Scrimmage does not know a gateway's address, so this page names it under `model_hosts`.
Without that line a call to the gateway is refused, and the test stops.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.
It was run against a scripted stand-in for the gateway, not a real one, and so not on a real model.

## The files

Start from the three files of the [function agent](function-agent.md).
Point the client at the gateway, which a LiteLLM proxy serves on port 4000 by default, and ask for the gateway's alias `smart`:

```diff title="shop/agent.py"
-    client = openai.OpenAI()
+    client = openai.OpenAI(base_url="http://localhost:4000/v1")
```

```diff title="shop/agent.py"
-        reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=SPECS)
+        reply = client.chat.completions.create(model="smart", messages=messages, tools=SPECS)
```

Then tell Scrimmage that `localhost` is a model host that speaks the OpenAI format:

```toml title="scrimmage.toml"
[agent]
function     = "shop.agent:answer"
tools        = ["shop.tools:get_order", "shop.tools:refund_order"]
model_hosts  = [{host = "localhost", provider = "openai"}]
max_cost_usd = 0.10

[agent.prices]
smart = {input = 0.15, output = 0.60}

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call   = "model"
model  = "smart"
do     = "status 429"
when   = "first 1"
expect = ["shipped", ["Oct 30", "October 30"]]
```

`model_hosts` is what makes the calls to `localhost` model calls: budgeted, and open to a fault by name.
Scrimmage sits in front of the gateway, so it sees the alias `smart` and never the model behind it, and the fault names the alias.
The gateway's alias has no shipped price, so `[agent.prices]` gives one.
A budget needs a price to hold money for each call before it is sent.

The fault fails the first call of each run with HTTP 429, and the `openai` library retries it.
The row passes if the bot still answers.

## What the gateway says a call cost

A gateway can state what a call cost: OpenRouter in `usage.cost`, a LiteLLM proxy in the `x-litellm-response-cost` header.
Scrimmage settles such a call at the larger of that cost and the cost of its tokens at your prices.
It adds nothing to the request to ask for it.
In this example the scripted gateway states $0.005 for each call, and the tokens at the prices above come to a few millionths of a dollar, so the stated cost is what the budget is charged.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261126T072759Z-8dfd32
test          row       fault                           runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                               3     -      3     3/3     100%  44-100%  1.0    0.03000  pass
order-status  fault 1   model smart status 429 first 1  3     3      3     3/3     100%  44-100%  1.0    0.03000  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0600 of a budget of $0.1
```

The fault row costs the same as the baseline: the faulted attempt is never sent, and its retry is.
The last line is 12 calls at the stated $0.005, not at the prices above.

Take out the `model_hosts` line and run it again.
The test stops with exit code 2.
To Scrimmage the request is a `POST` to a web host it does not know as a model host, so it is refused as a web write, and the message names the line to add:

```text
test mistake: order-status baseline: run-6a503f73187b: http:localhost POST /v1/chat/completions is a model request to a host not under [agent] model_hosts, so it was refused as a web write. Add {host = "localhost", provider = "openai"} to [agent] model_hosts; a model call listed under real is never budgeted or faulted by model name.
```

More: [Use a gateway](../model-faults.md#use-a-gateway), [Prices](../prices.md), [The budget](../budget.md).
