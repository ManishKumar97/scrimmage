# Quickstart

This walks through a small order bot and one test: what does the bot say when its `get_order` tool times out?

## 1. The agent

The bot is a Python function with its own loop.
It asks the model, runs the tools the model asks for, and returns the answer.

```text
acme/
  agent.py    # def answer(question: str) -> str
  tools.py    # get_order, check_inventory, refund_order, get_stock
  models.py   # Stock, a small dataclass
```

```python
# acme/agent.py (shortened)
from acme import tools


def answer(question: str) -> str:
    ...
    result = getattr(tools, call.function.name)(**arguments)
    ...
```

Nothing in this code mentions Derail.
The full example is in the repository under `tests/example/`.

## 2. The test file

Next to the code, write `derail.toml`:

```toml
[agent]
function = "acme.agent:answer"
tools    = ["acme.tools:get_order", "acme.tools:check_inventory",
            "acme.tools:refund_order", "acme.tools:get_stock"]
real     = ["tool:check_inventory"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"
returns = {sku = "SKU-42", count = 12}   # becomes an acme.models.Stock

[[test.fault]]
call = "tool:get_order"
do   = "timeout"
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30", "October 30"]
```

This says:

- run `acme.agent:answer` with the question;
- `get_order` returns "shipped, Oct 30", and `get_stock` returns a `Stock` with 12 in stock, instead of calling real systems;
- `check_inventory` is safe to run for real;
- `refund_order` is neither mocked nor safe, so if the agent calls it, the call is refused;
- the answer must say "shipped" and give the date, in either spelling;
- then try the same test again with `get_order` timing out. There the answer must admit the failure and give no date.

## 3. Run it

First check the file. This runs nothing and costs nothing:

```bash
derail check
```

```text
derail check: derail.toml is valid: 1 test, 2 rows, 6 runs, 4 tools, a budget of $5
```

Then run the test:

```bash
derail test
```

```text
derail test: 1 test, 2 rows, 6 runs; records in derail-runs/20261105T075653Z-e277a2
test          row       fault                   runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                       3     -      3     3/3     100%  44-100%  2.0    0.00015  pass
order-status  fault 1   tool:get_order timeout  3     3      3     3/3     100%  44-100%  2.0    0.00015  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0003 of a budget of $5
```

Derail ran the test as two **rows**: a baseline with the mocks and no fault, and one row with the timeout.
Each row ran three times, each time as a brand-new program.
In all three fault runs the timeout fired, and the bot admitted it, so both rows pass.
The rate and its range say what happened in these three runs, not what the next run will do.
If a row fails, the lines under the table name the rule that broke. See [Judging an answer](judging.md).

## 4. Read what happened

Each run has a record in `derail-runs/`, one JSON object per line:

```json
{"call": "model:openai", "action": "real", "model": "gpt-4o-mini", "status": 200, "cost_usd": 0.0000225, ...}
{"call": "tool:get_order", "action": "fault", "fault": {"do": "timeout", "when": "always", "n": 1},
 "error": "TimeoutError: get_order timed out", ...}
{"call": "tool:get_stock", "action": "mock", ...}
{"summary": {"outcome": "answered", "answer": "...", "fault_fired": true, "judged": "pass", ...}}
```

The answer is in the summary line, so you can read what the bot said, and `judged` says how that run counted.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every row passed |
| 1 | A row failed |
| 2 | A mistake in the test: a refused call, a fault that never fired, a mock that does not fit its type, a misspelled label, a failed baseline, a row with no result, or a test the budget stopped |
| 3 | An infrastructure error: a run whose record is incomplete, an error from the provider nobody injected, or a failure inside Derail itself |

Next: [Failing the model, and keeping a budget](model-faults.md), [An agent run as a script](scripts.md) if yours is a program and not a function, or [Concepts](concepts.md).
