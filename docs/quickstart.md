# Quickstart

This page takes you from a fresh clone to a test that fails, and then passes after a fix.
It uses the small order bot in the repository, under `tests/example`.

You need Python 3.11 or newer, an OpenAI API key for the example bot, and about five minutes.
An agent on Anthropic's library works the same way; see [An agent that uses the anthropic library](model-faults.md#an-agent-that-uses-the-anthropic-library).

If you already have an agent, start from your own code: run `scrimmage init` in its folder, and it writes a first `scrimmage.toml` from reading the code, with no import and no run.
See [Drafting a test file](init.md).
The rest of this page walks through the example with a file already written.

## 1. Get the example and install Scrimmage

```bash
git clone https://github.com/ManishKumar97/scrimmage
cd scrimmage
python -m venv .venv
. .venv/bin/activate
pip install ./packages/scrimmage openai
export OPENAI_API_KEY=...   # your own key
cd tests/example
```

The bot calls OpenAI through the `openai` library, so that library is installed too.
Scrimmage itself needs nothing outside Python's standard library.
See [Install](install.md) for other ways to install it.

## 2. The agent

The bot is a Python function with its own loop.
It asks the model, runs the tools the model asks for, and returns the answer.

```text
acme/
  agent.py    # def answer(question: str) -> str
  tools.py    # get_order, check_inventory, refund_order, get_stock
  models.py   # Stock, a small dataclass
```

```python
# acme/agent.py, shortened
def run_tool(name: str, arguments: str) -> str:
    try:
        result = getattr(tools, name)(**json.loads(arguments))
    except Exception as exc:  # the bot tells the model what went wrong
        return f"error: {type(exc).__name__}: {exc}"
    ...


def answer(question: str) -> str:
    return loop(openai.OpenAI(max_retries=0), question)
```

Nothing in this code mentions Scrimmage.

## 3. The test file

Next to the code is `scrimmage.toml`:

```toml
[agent]
function = "acme.agent:answer"
tools    = ["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:refund_order", "acme.tools:get_stock"]
real     = ["tool:check_inventory"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"             # returns an acme.models.Stock object
returns = {sku = "SKU-42", count = 12}

[[test.fault]]
call = "tool:get_order"
do   = "timeout"
expect = [["unable", "not able", "wasn't able", "was not able", "can't", "cannot", "couldn't", "could not", "don't have access", "do not have access", "i don't have", "i don't know", "timed out", "ran into", "sorry", "unfortunately", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "issue retrieving", "issue with retrieving", "timeout issue", "i'm experiencing", "i am experiencing", "i'm currently experiencing"]]
forbid = ["shipped", "ships", "will ship", "ship soon", "ship today", "ship tomorrow", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "april", "june", "july", "august", "september", "december"]
```

The file in the repository has one more line, `model_hosts`.
It lets the repository's own tests send model calls to a fake model server on `127.0.0.1`.
It changes nothing when you use a real key.

This file says:

- run `acme.agent:answer` with the question;
- `get_order` returns "shipped, Oct 30", and `get_stock` returns a `Stock` with 12 in stock, so no real system is called;
- `check_inventory` is safe to run for real;
- `refund_order` is neither mocked nor safe, so if the agent calls it, the [gated](concepts.md#gate) call is refused;
- the answer must say "shipped" and give the date, in either spelling;
- then run the same test with `get_order` timing out.
  There the answer must admit the failure, and must not say "shipped" or give a date.
  The list of words is long on purpose.
  A real model has many ways to say it could not, and a rule that is too narrow fails a run that did the right thing.
  The list checks wording, not truth: it cannot tell "I could not look it up" from "sorry, it was delivered".
  `forbid` is the guard against a made-up answer, so it lists what a bot with no data would invent.
  The list was written against `gpt-4o-mini`, so rerun it on your own model and add its phrases.
  Never widen it with a word that can sit in a sign-off, such as "issue" or "problem".

## 4. Run it

First check the file. This runs nothing and costs nothing:

```bash
scrimmage check
```

```text title="Output"
scrimmage check: scrimmage.toml is valid: 1 test, 2 rows, 6 runs, 4 tools, a budget of $5, OpenAI prices as of 2026-11-22, Anthropic prices as of 2026-11-25
```

Then run the test:

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193000Z-4c1d2a
test          row       fault                   runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                       3     -      3     3/3     100%  44-100%  2.0    0.00016  pass
order-status  fault 1   tool:get_order timeout  3     3      3     3/3     100%  44-100%  2.0    0.00016  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0003 of a budget of $5
```

Scrimmage ran the test as two **rows**: a baseline with the mocks and no fault, and one row with the timeout.
Each row ran three times, each time as a new program.
In all three fault runs the timeout fired, and the bot said it could not look up the order, so both rows pass.
The rate and its range describe these three runs only.
They do not predict the next run.

Your answers will use different words, and your cost will differ a little.

## 5. Add a fault the bot does not handle

What happens when the model itself is busy and answers with HTTP 429, "too many requests", just once?
Add a second fault at the end of `scrimmage.toml`:

```diff title="scrimmage.toml"
+
+[[test.fault]]
+call = "model"
+do   = "status 429"
+when = "first 1"
```

`first 1` fails only the first model call of each run.
Scrimmage answers that call itself with a 429 reply shaped like the provider's, so nothing reaches OpenAI and it costs nothing.
This fault row has no rules of its own, so the answer must pass the test's own rules: after one busy reply, the bot should still answer.

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 3 rows, 9 runs; records in /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193100Z-9e0b17
test          row       fault                     runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                         3     -      3     3/3     100%  44-100%  2.0    0.00016  pass
order-status  fault 1   tool:get_order timeout    3     3      3     3/3     100%  44-100%  2.0    0.00016  pass
order-status  fault 2   model status 429 first 1  3     3      0     0/3     0%    0-56%    0.0    0.00000  fail
range: a 95% Wilson interval, from these runs alone
fail: order-status fault 2: 0/3 passed, pass_rate is 1
  3 runs: crashed: /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193100Z-9e0b17/run-312d14f54352.log, /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193100Z-9e0b17/run-a5fa971e377c.log, /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193100Z-9e0b17/run-355cdc9d4a47.log
model calls cost $0.0003 of a budget of $5
note: order-status fault 2: 3 crashed
```

The new row fails, and `scrimmage test` exits with code 1.
Every run crashed.
The log of a run ends with the error your agent raised:

```text
openai.RateLimitError: Error code: 429 - Rate limit reached for default-codex in organization org-{id} on requests per min. ...
```

The body of that 429 is a saved real reply, so it names that reply's model and not yours.

The bot builds its client with `max_retries=0`, so the first busy reply ends the whole answer.

## 6. Fix the bot

Let the `openai` library retry, as it does by default:

```diff title="acme/agent.py"
-    return loop(openai.OpenAI(max_retries=0), question)
+    return loop(openai.OpenAI(max_retries=2), question)
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 3 rows, 9 runs; records in /home/me/scrimmage/tests/example/scrimmage-runs/20261107T193200Z-27f0c3
test          row       fault                     runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                         3     -      3     3/3     100%  44-100%  2.0    0.00016  pass
order-status  fault 1   tool:get_order timeout    3     3      3     3/3     100%  44-100%  2.0    0.00016  pass
order-status  fault 2   model status 429 first 1  3     3      3     3/3     100%  44-100%  2.0    0.00016  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0005 of a budget of $5
```

Every row passes, and `scrimmage test` exits with code 0.
In each run the first model call got the 429, the library waited and retried, and the retry went through.
Keep the fault row in the file, so a later change that turns retries off again fails the test.

## 7. Read what happened

Each run has a record in `scrimmage-runs/`, one JSON object per line:

```json
{"call": "model:openai", "action": "fault", "fault": {"do": "status 429", "n": 1, "when": "first 1"}, "model": "gpt-4o-mini", "status": 429, ...}
{"call": "model:openai", "action": "real", "model": "gpt-4o-mini", "status": 200, "cost_usd": 0.0000225, ...}
{"call": "tool:get_order", "action": "mock", ...}
{"summary": {"outcome": "answered", "answer": "...", "fault_fired": true, "judged": "pass", ...}}
```

The last line is the summary.
It holds the answer, and `judged` says how that run counted.
See [Judging an answer](judging.md) for the rules, and [Concepts](concepts.md) for rows, runs and records.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every row passed |
| 1 | A row failed |
| 2 | A mistake in the test: a refused call, a fault that never fired, a mock that does not fit its type, a misspelled label, a failed baseline, a row with no result, or a test the budget stopped |
| 3 | An infrastructure error: a run whose record is incomplete, an error from the provider nobody injected, or a failure inside Scrimmage itself |

Next: the [examples](examples/index.md), [Failing the model](model-faults.md), [The budget](budget.md), or [An agent run as a script](scripts.md) if your agent is a program and not a function.
