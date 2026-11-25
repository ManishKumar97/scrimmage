# The budget

This page covers the spending limit of a test: `max_cost_usd`, how a call is held and settled, and what happens when calls wait or are refused.
The prices a call is held and settled at are on the [Prices](prices.md) page.

## Keep a budget

Every test has a budget for the whole run of `scrimmage test`. It is $5 unless you change it:

```toml
[agent]
max_cost_usd = 0.50
```

A fault row can cost far more than the baseline, such as an agent that retries a failing write in a loop.
`run_timeout` ends a looping run, and `max_cost_usd` caps the whole test.

Before each model call, Scrimmage reserves an estimate of what the call could cost: its input at the full input price, or at the dearest cache-write price its API allows if that is higher, plus its `max_tokens` (or `reserve_output_tokens`, 4096 by default) at the output price, times `n` on Chat Completions, which bills the output of every choice.
After the call, the real cost from the reply replaces the reservation.
Scrimmage reads it from the reply's usage, after undoing `gzip`, `deflate` or `br`; for `br` it uses your program's own `brotli` or `brotlicffi` 1.2 or later, which is also what makes httpx ask for `br`.
So calls at once cannot together pass the limit while each costs no more than it reserved.
A call can cost more: a reply with no output limit that writes more than `reserve_output_tokens`, or an input with more tokens than the estimate, such as an image.
Then the limit can be passed by what the calls cost above their reservations, added together; see [A call can cost more than its hold](limits/budget.md#the-budget).
The real cost prices the input tokens the provider served from its prompt cache at the cached price, as OpenAI bills them.

To turn the budget off on purpose:

```toml
max_cost_usd = "none"
```

### When calls in flight hold the budget

A hold can be much larger than what the call costs.
On `gpt-4.1`, 4096 output tokens at $8 a million hold about $0.033, so three calls in flight hold all of a $0.10 budget while almost nothing is spent.
A call that needs room then waits for the others to finish and give back what they did not use:

| Money spent plus this call | Spent, held and this call | What happens |
|---|---|---|
| over the limit | any | refused at once: waiting cannot help, as spent money never comes back |
| within the limit | within the limit | the call goes ahead |
| within the limit | over the limit | the call waits for calls in flight to finish, then goes ahead |

A waiting call takes a ticket and asks again when the runner says to.
The first in line asks again within a quarter of a second, and each call ahead adds a twentieth of a second, up to 2 seconds, with some random spread so calls that started together do not ask together.
So 100 waiting calls ask about 100 times a second, not 400, and the runner stays quick to answer the calls that finish.
Tickets are served in the order they came, so a large call is not starved by a stream of small ones.
A call is granted when it fits along with every call ahead of it in line, so many calls that all fit go ahead together.
An async call waits on its event loop, not on a thread, so asyncio's threads stay free for other work, such as looking up host names.
How long a call waits depends on whose calls hold the room:

| The holds in the way | The longest wait | Why |
|---|---|---|
| this run's own calls would keep it out even if every other run finished | 30 seconds, or half the time the run has left, if that is less | the run may be waiting on itself |
| other runs' calls | until 10 seconds before this run's `run_timeout`, or a tenth of it, if that is less | the other run's own time limit ends its holds |
| a run that has ended | no wait: refused at once | that hold can never be given back |

If the call is still held out then, it is refused and the test stops with exit code 2.
The time a run has left counts from when Scrimmage started its program, as `run_timeout` does.
A slow start-up before the SDK loads, such as `uv run` on a cold cache or a setup script, uses up part of that time, so the wait still ends before the run is stopped.
A run waits on itself when a hold cannot end until the waiting call returns.
A stream the agent keeps open across a second call does that, and so does a stream it stopped reading without closing: each keeps its hold until it is closed, or the run ends.
Close a stream, or read it to its end, before the next call.

The refusal says what the call could cost, what was spent, which calls held the rest, with their run, their label and what each held, and how long it waited.
When spent money cannot cover a call, the call is refused at once.

| How a run ends | When |
|---|---|
| "budget reached" | money spent reached the limit |
| "not reserved" | the call could not fit, though money spent had not reached the limit: the money left was too little, or calls in flight held it |

The table's last line says what the test spent, and a `test mistake:` line says why the test stopped:

| What stopped the test | The line |
|---|---|
| money spent reached the limit | `the budget of $0.1 was reached: $0.1003 was spent, so the test stopped` |
| a call could not fit, though spent money had not reached the limit | `a model call could not be reserved: it could cost up to $0.0331, $0.0800 was spent and $0.0000 was held by calls in flight, of a budget of $0.1, so the test stopped; ...` |
| a call waited for calls in flight and still could not fit | the same, with the holds named, such as `(model:openai in run-3f2a $0.0331)`, and `after a wait of 30s for calls in flight` |

A run killed while one of its calls holds money, such as one that ignored SIGTERM, leaves that hold in place, as the call may have been sent.
It counts as spent from then on: a call it keeps out is refused at once, and the refusal names it as `left by run-3f2a, which ended while still holding`.
The report then warns: `warning: run-3f2a ended with model:openai still holding $0.0331 of the budget`.

To hold less, set `max_tokens` on the call, lower `reserve_output_tokens`, or lower `parallel`.
