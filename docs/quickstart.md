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

Nothing in this code mentions Faultline.
The full example is in the repository under `tests/example/`.

## 2. The test file

Next to the code, write `faultline.toml`:

```toml
[agent]
function = "acme.agent:answer"
tools    = ["acme.tools:get_order", "acme.tools:check_inventory",
            "acme.tools:refund_order", "acme.tools:get_stock"]
real     = ["tool:check_inventory"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", "Oct 30"]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"
returns = {sku = "SKU-42", count = 12}   # becomes an acme.models.Stock

[[test.fault]]
call = "tool:get_order"
do   = "timeout"
```

This says:

- run `acme.agent:answer` with the question;
- `get_order` returns "shipped, Oct 30", and `get_stock` returns a `Stock` with 12 in stock, instead of calling real systems;
- `check_inventory` is safe to run for real;
- `refund_order` is neither mocked nor safe, so if the agent calls it, the call is refused;
- then try the same test again with `get_order` timing out.

## 3. Run it

```bash
faultline test
```

```text
faultline test: 1 test, 2 rows, 6 runs; records in faultline-runs/20261105T075653Z-e277a2
test          row       fault                   runs  exercised  answered  crashed  timed out  refused call  budget reached  infrastructure error
order-status  baseline  -                       3     -          3         0        0          0             0               0
order-status  fault 1   tool:get_order timeout  3     3          3         0        0          0             0               0
model calls cost $0.0003 of a budget of $5
```

Faultline ran the test as two **rows**: a baseline with the mocks and no fault, and one row with the timeout.
Each row ran three times, each time as a brand-new program.
In all three fault runs the timeout fired ("exercised"), and the bot still answered.

## 4. Read what happened

Each run has a record in `faultline-runs/`, one JSON object per line:

```json
{"call": "model:openai", "action": "real", "model": "gpt-4o-mini", "status": 200, "cost_usd": 0.0000225, ...}
{"call": "tool:get_order", "action": "fault", "fault": {"do": "timeout", "when": "always", "n": 1},
 "error": "TimeoutError: get_order timed out", ...}
{"call": "tool:get_stock", "action": "mock", ...}
{"summary": {"outcome": "answered", "answer": "...", "fault_fired": true, ...}}
```

The answer is in the summary line, so you can see whether the bot admitted the outage or made something up.
Judging answers against `expect` comes in a later version.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every row produced a result |
| 2 | A mistake in the test: a refused call, a fault that never fired, a mock that does not fit its type, a misspelled label, a failed baseline, or a test the budget stopped |
| 3 | An infrastructure error: a run whose record is incomplete, an error from the provider nobody injected, or a failure inside Faultline itself |

Next: [Failing the model, and keeping a budget](model-faults.md), or [Concepts](concepts.md).
