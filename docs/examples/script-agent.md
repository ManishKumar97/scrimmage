# A script and its child process

Some agents are a program you start with a question, which prints its answer and ends.
This example turns the [function agent](function-agent.md) into a script that also starts a child process.
The child calls a tool that writes, and Scrimmage's rules reach it there too.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.

## The files

Start from the three files of the [function agent](function-agent.md).
Add a tool that writes:

```diff title="shop/tools.py"
+
+
+def notify_customer(order_id: str) -> str:
+    """Email the customer about their order. This sends a real email."""
+    raise RuntimeError("a real email must never be sent from a test")
```

The script prints the answer, then starts a child that emails the customer:

```python title="bot.py"
"""The bot as a script: python bot.py "question". It prints its answer, then starts a child."""

import subprocess
import sys

from shop.agent import answer


def main() -> None:
    print(answer(sys.argv[1]), flush=True)
    subprocess.run([sys.executable, "notify.py", "1234"], check=True)


if __name__ == "__main__":
    main()
```

```python title="notify.py"
"""A child process. It prints nothing, so the bot's answer stays the last line printed."""

import sys

from shop import tools

tools.notify_customer(sys.argv[1])
```

`command` replaces `function`, with `{question}` where the question goes:

```toml title="scrimmage.toml"
[agent]
command = "python bot.py {question}"
tools   = ["shop.tools:get_order", "shop.tools:refund_order", "shop.tools:notify_customer"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"
```

Scrimmage starts the program once per run, with no shell, so the question reaches `bot.py` as one argument.
The answer is the last line the program printed.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 1 row, 3 runs; records in /home/me/shop/scrimmage-runs/20261107T194500Z-5d6e7f
test          row       fault  runs  fired  done  passed  rate  range  tools  cost     result
order-status  baseline  -      3     -      0     0/0     -     -      2.0    0.00009  no result
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0001 of a budget of $5
note: order-status baseline: 0 of 3 runs judged
note: order-status baseline: 3 refused call
test mistake: order-status baseline: run-5991c0c83717 made a call the test does not allow; see its record
test mistake: order-status baseline: run-5991c0c83717: tool:notify_customer is neither mocked by this test nor listed under [agent] real. Mock it in the test, or add "tool:notify_customer" to real if it is safe to run for real.
test mistake: order-status baseline: run-08e6c91b40d7 made a call the test does not allow; see its record
test mistake: order-status baseline: run-08e6c91b40d7: tool:notify_customer is neither mocked by this test nor listed under [agent] real. Mock it in the test, or add "tool:notify_customer" to real if it is safe to run for real.
test mistake: order-status baseline: run-ac4f3faee7f2 made a call the test does not allow; see its record
test mistake: order-status baseline: run-ac4f3faee7f2: tool:notify_customer is neither mocked by this test nor listed under [agent] real. Mock it in the test, or add "tool:notify_customer" to real if it is safe to run for real.
```

The child joined the run, so its call to `notify_customer` was refused like any other.
No email was sent, and no run is judged, because each one stopped at the refusal.
A refused call is a mistake in the test, so `scrimmage test` exits with code 2.

## Mock the write

Give the tool a fixed reply in the test:

```diff title="scrimmage.toml"
+
+[[test.mock]]
+call    = "tool:notify_customer"
+returns = "sent"
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 1 row, 3 runs; records in /home/me/shop/scrimmage-runs/20261107T194600Z-8a9b0c
test          row       fault  runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -      3     -      3     3/3     100%  44-100%  2.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0001 of a budget of $5
```

The record of each run lists the child process and its mocked call.
To fire faults inside a child too, set `children = true`.

More: [An agent run as a script](../scripts.md), which covers the answer file, child processes Scrimmage cannot reach, and how a run stops.
