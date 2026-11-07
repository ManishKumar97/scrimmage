# Judging an answer

Each run's answer is judged by rules you write: `expect`, `forbid` and `pass_rate`.
Scrimmage compares text, with no model, so the same answer always gets the same result.
This example shows a rule that is too narrow, what the report says about it, and the fix.

## The files

Start from the three files of the [function agent](function-agent.md), and replace its test:

```toml title="scrimmage.toml"
[agent]
function = "shop.agent:answer"
tools    = ["shop.tools:get_order", "shop.tools:refund_order"]
runs     = 5

[[test]]
name      = "order-status"
question  = "Where is order 1234?"
expect    = ["delivered", ["Oct 30", "October 30"]]
forbid    = ["refund"]
pass_rate = 0.8

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"
```

| Rule | Means |
|---|---|
| `expect` | Every item must be in the answer. An item that is a list, such as `["Oct 30", "October 30"]`, needs any one of its texts. |
| `forbid` | None of these texts may be in the answer. |
| `pass_rate` | The share of judged runs that must pass for the row to pass. Here 4 of 5. Every run by default. |

Case is ignored, and so are differences in spaces and Unicode forms.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 1 row, 5 runs; records in /home/me/shop/scrimmage-runs/20261107T200500Z-2e3f4a
test          row       fault  runs  fired  done  passed  rate  range  tools  cost     result
order-status  baseline  -      5     -      5     0/5     0%    0-43%  1.0    0.00016  fail
range: a 95% Wilson interval, from these runs alone
fail: order-status baseline: 0/5 passed, pass_rate is 0.8
  5 runs: expect "delivered" not found: run-5fd356643999, run-d2fb95087ca1, run-17e34ee91399 and 2 more
model calls cost $0.0002 of a budget of $5
```

The row fails.
The lines under the table name the rule that broke and the runs it broke in.
Each run's answer is in the summary line of its record, so you can see whether the agent or the rule is at fault.
Here the answer said the order shipped on Oct 30, which is right: it is shipped, not yet delivered.
The rule asked for a word the right answer does not use.

## Fix the rule

```diff title="scrimmage.toml"
-expect    = ["delivered", ["Oct 30", "October 30"]]
+expect    = [["shipped", "delivered"], ["Oct 30", "October 30"]]
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 1 row, 5 runs; records in /home/me/shop/scrimmage-runs/20261107T200600Z-5b6c7d
test          row       fault  runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -      5     -      5     5/5     100%  57-100%  1.0    0.00016  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $5
```

Rules find text, with no sense of meaning.
"shipped" is also found in "has not shipped", so pair a rule with a `forbid` when an answer that should fail could hold the same word.
A rate describes the runs that happened, and a few runs say little.
The range in the table shows how little.

More: [Judging an answer](../judging.md), which covers how text is compared, rules on fault rows, and every result a row can have.
