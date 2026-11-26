# A budget that stops a looping agent

A fault row can cost far more than the baseline.
A tool fails, the model asks the bot to try again, the bot asks the model, and so on until the loop ends.
Every turn is a model call that costs money.
`max_cost_usd` caps what the whole test may spend, and Scrimmage stops the test when the cap is reached.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.
The repository's tests run it on a scripted model that asks again after every failure, as a bot with a retry loop does, and that reasons before each tool call.
The release check runs it once on `gpt-4o-mini`, where the model may answer after the first failure and never reach the cap.
Then the page's numbers differ, and the check asserts only that the test stops at or under its budget.

## The files

Start from the three files of the [function agent](function-agent.md).
The bot's loop already asks the model again after a failed tool, up to 8 turns.
Replace `scrimmage.toml` to set a budget, and a price of your own:

```toml title="scrimmage.toml"
[agent]
function              = "shop.agent:answer"
tools                 = ["shop.tools:get_order", "shop.tools:refund_order"]
max_cost_usd          = 0.06
reserve_output_tokens = 64
parallel              = 1

[agent.prices]
gpt-4o-mini = {input = 10.0, output = 30.0}

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

- `max_cost_usd` is $0.06 for the whole `scrimmage test`, not for each run.
  The default is $5.
- `[agent.prices]` gives `gpt-4o-mini` a price of your own, in dollars per million tokens: $10 for input and $30 for output.
  It is read before the shipped price, so it wins.
  The real price is far lower, so this short test spends enough to meet the cap.
- `reserve_output_tokens` is what a call holds before it is sent, when it sets no `max_tokens`.
  At the default of 4096 and $30 a million, one call would hold $0.12 and the cap could never fit it.
  Here it is 64, so a call holds about $0.007.
- `parallel = 1` runs one run at a time, so the point where the cap is reached does not depend on timing.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 4 runs; records in /home/me/shop/scrimmage-runs/20261126T085802Z-9ee666
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.03993  pass
order-status  fault 1   tool:get_order error  1     1      0     0/0     -     -        2.0    0.02564  no result
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0656 of a budget of $0.06
warning: the test spent more than its budget of $0.06, because the last calls cost more than was reserved for them
note: order-status fault 1: 1 budget reached
test mistake: order-status fault 1: no result: 0 of 3 runs were judged, and a result needs 2
test mistake: the budget of $0.06 was reached: $0.0656 was spent, so the test stopped; rows without runs have no result
```

The baseline passed, and its 3 runs cost $0.04.
In the fault row the first run asked the model again after each failure.
Each reply cost $0.0128, more than the $0.007 a call held, because the model reasoned before it called the tool.
After 2 turns the money spent, $0.0656, was over the cap, and Scrimmage refused the next call: "budget reached".
The test stopped with exit code 2, and the fault row has no result, as only one run began.

The cap is passed by what the last calls cost above their holds, and by no more than that.
Scrimmage cannot know how long a reply will be before it is sent, and the `warning:` line says so.
The rest of the loop never ran: the 6 turns left in the bot's 8 cost nothing.


## A model with no price

A price is how Scrimmage holds money for a call before it is sent, so a model with no price is refused.
Say the bot moves to a model of your own, `acme-llm`, which neither `[agent.prices]` nor the shipped prices name:

```diff title="shop/agent.py"
-        reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=SPECS)
+        reply = client.chat.completions.create(model="acme-llm", messages=messages, tools=SPECS)
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 1 run; records in /home/me/shop/scrimmage-runs/20261126T085920Z-9606ee
test          row       fault                 runs  fired  done  passed  rate  range  tools  cost     result
order-status  baseline  -                     1     -      0     0/0     -     -      0.0    0.00000  no result
order-status  fault 1   tool:get_order error  0     0      0     0/0     -     -      0.0    0.00000  baseline failed
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0000 of a budget of $0.06
note: order-status baseline: 0 of 1 runs judged; 3 were planned
note: order-status baseline: 1 refused call
test mistake: order-status baseline: run-aeaa4044c364 made a call the test does not allow; see its record
test mistake: order-status baseline: run-aeaa4044c364: Scrimmage has no price for the model 'acme-llm', so it cannot keep the budget; add it under [agent.prices], such as "acme-llm" = {input = 0.15, output = 0.60} (dollars per million tokens), or set max_cost_usd = "none"
test mistake: order-status fault 1: no result: 0 of 3 runs were judged, and a result needs 2
test mistake: order-status: baseline failed: every baseline run crashed, timed out, was refused, reached the budget or hit an infrastructure error, so its fault rows did not run
```

No call was sent: Scrimmage refused it first, and the message gives the line to paste under `[agent.prices]`.
Scrimmage never guesses a price, because a guess too low would let a test spend more than its cap.

More: [The budget](../budget.md), [Prices](../prices.md).
