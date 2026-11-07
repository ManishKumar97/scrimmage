# Failing the model, and keeping a budget

This guide adds two things to the [quickstart](quickstart.md) test: the model itself fails, and the test has a spending limit.

## Make the model fail

Add a fault on the `model` label:

```toml
[[test.fault]]
call = "model"
do   = "status 429"
when = "first 3"
```

Derail answers the model call with a real HTTP 429, without touching the network.
The openai library reads it exactly as it would read a real one.

Why `first 3`?
The openai library retries a 429 twice by itself before your code sees anything.
Each retry meets the gate again, so three failures are needed to get past the library:

| Attempt | What happens |
|---|---|
| 1 | Derail answers 429; the library waits and retries |
| 2 | Derail answers 429; the library waits and retries |
| 3 | Derail answers 429; the library gives up and raises `RateLimitError` into your code |

With `first 1`, the first retry goes through and your agent never sees an error, which mainly tests the library.

Nothing is sent to the provider for a faulted attempt, so it costs nothing.

## Faults you can use on the model

| Fault | What your agent sees |
|---|---|
| `status 429` | the library's `RateLimitError`, after its retries |
| `status 500` | the library's `InternalServerError`, after its retries |
| `status 400` | the library's `BadRequestError`, at once |
| `timeout` | the library's `APITimeoutError`, after its retries |

Any status from 400 to 599 works.

## Choosing when it fires

| `when` | Fires on | Use it for |
|---|---|---|
| `always` (default) | every matching call | an outage that never ends: does the agent admit it? |
| `first N` | the first N attempts, retries included | an outage that ends: does the agent recover? |
| `requests N` | the first N requests, and every retry of each | failing a whole request however many retries the library makes |

`first 3` depends on the library retrying exactly twice.
If your client sets its own `max_retries`, use `requests 1`: it fails the request and all its retries, whatever their number.

Calls are counted per run.
When two model calls run at once, such as a guardrail next to the main call, Derail matches each retry to its request by the thread or task that sent it and the request's content, so each request keeps its own number.

## Which model calls a label names

| Label | Means |
|---|---|
| `model` | every chat call, on any provider |
| `model:openai` | chat calls to OpenAI |
| `model:openai/embeddings` | embedding calls to OpenAI |
| `model:*/embeddings` | embedding calls on any provider |

A fault on `model` leaves embeddings alone, so you can fail the chat and keep your retrieval working, or the other way around.
To tell two models apart, see [Failing one model](#failing-one-model).

Model calls never need listing under `real`, and they cannot be mocked: the test exists to see how the real model reacts.

## Failing one model

A label names every model call of its kind, so a bare `model` fault also hits a router, a planner, a guardrail or a summariser.
Add `model` to fail one model and leave the others alone:

```toml
[[test.fault]]
call  = "model"
model = "gpt-4.1"
do    = "status 429"
when  = "first 3"
```

A bot that routes with `gpt-4o-mini` and answers with `gpt-4.1` then gets this:

| Call | What happens |
|---|---|
| `gpt-4o-mini` | runs for real and is not counted |
| `gpt-4.1`, attempt 1 | Derail answers 429; the library retries |
| `gpt-4.1`, attempt 2 | Derail answers 429; the library retries |
| `gpt-4.1`, attempt 3 | Derail answers 429; the library raises `RateLimitError` into your code |

`first N`, `requests N` and `always` count only the calls to that model, so a router call in between takes none of the count.
It works on chat, Responses and embeddings, sync or async, with the reply streamed or not.
For embeddings, name them in the label too:

```toml
call  = "model:*/embeddings"
model = "text-embedding-3-large"
```

The table shows the name after the label, such as `model gpt-4.1 status 429 first 3`, so two rows read apart.
On a record line, `call` stays `model:openai`, and the line's `model` field says which model it was.

### The name is compared whole

Write the name your code sends, case and all, not the name a reply or a dashboard shows.
There are no prefixes, because `gpt-4o` would then hit a router on `gpt-4o-mini` as well, and nothing would tell you.

| `model` in the fault | Name in the request | Fires? |
|---|---|---|
| `gpt-4.1` | `gpt-4.1` | yes |
| `gpt-4.1` | `gpt-4.1-mini` | no |
| `gpt-4o-mini` | `gpt-4o-mini-2024-07-18` | no |
| `smart`, a gateway's alias | `smart` | yes |

If your code sends a dated name such as `gpt-4o-mini-2024-07-18`, write the dated name.
A gateway's alias works the same way: Derail sits in front of the gateway, so it sees the alias and never the name behind it.

A wildcard is refused when the file is read, and so is a space at either end of the name or a character that does not print.

A name no call used is a fault that never fired, so it is a test mistake with exit code 2 and the row has `no result`.
It can never read as a pass, and the message says what the calls did use:

```
test mistake: routed fault 1: no call used the model 'gpt-4.2'; these calls used 'gpt-4.1', 'gpt-4o-mini'
```

That catch needs no call to have sent the exact name.
If your code sends both `gpt-4o-mini` and `gpt-4o-mini-2024-07-18`, a fault on the first fires on it alone, the dated call runs for real, the row can pass, and nothing warns.

If the name was used and the fault still did not fire, the message says that instead: a refused call fires no fault, and nor does a call in a child process unless `children` is true.
When the records cannot say, because a call had no readable name or a run was refused, the message is left out.

### A request with no name Derail can read

Derail reads the name from the `model` field of the request body.
That reading changes nothing: the request goes out byte for byte as your code built it, so the provider's prompt cache is not disturbed.

When a fault gives `model` and a call its label names has no such field, Derail cannot tell whether the fault is for that call.
So the call is refused before it is sent, with the reason `unreadable_model`, and the test stops with exit code 2.
That covers a request body sent from a generator, a multipart or compressed body, and a body with no `model` at all, such as a Responses call on a stored prompt.
Firing could hit the wrong call, and letting it through could spare the right one.

For a stored prompt, send `model` on that call.
The openai library takes `model` beside `prompt`, but Derail has not confirmed what the provider does when the stored prompt names another model.

Taking `model` off the fault is the other way out, and it has two costs.
The fault hits every call its label names again, such as the router.
And with a budget on, a chat or an embedding with no name has no price, so it is refused in the baseline too and the fault row never runs.
Such a test needs `max_cost_usd = "none"`.

Calls the fault's label does not name go on as usual, and so does everything in a test with no `model` on its fault.

## Keep a budget

Every test has a budget for the whole run of `derail test`. It is $5 unless you change it:

```toml
[agent]
max_cost_usd = 0.50
```

Before each model call, Derail reserves what the call could cost: an estimate of its input, plus its `max_tokens` (or `reserve_output_tokens`, 4096 by default) at the output price.
After the call, the real cost from the reply replaces the reservation.
So four calls at once cannot all slip past the limit.

When the budget cannot cover a call, the call is not made, that run ends as "budget reached", and the test stops with exit code 2.
The table's last line says what the test spent.

To turn the budget off on purpose:

```toml
max_cost_usd = "none"
```

### Prices

Derail ships prices for common OpenAI models.
A dated model, such as `gpt-4o-mini-2024-07-18`, uses its family's price.
For any other model, give the price in dollars per million tokens:

```toml
[agent.prices]
llama-70b = {input = 0.6, output = 0.6}
```

With a budget on, a model with no price stops the test at its first call, because it cannot be reserved for.
With `max_cost_usd = "none"`, no price is needed, and tokens are still counted.

A reply without usable token counts gets a rough cost, worked out from how many bytes went each way, and its record line has `cost_estimated` set.
On Chat Completions a stream only carries counts if your request sets `stream_options.include_usage`. Derail leaves your request as it is, so without that setting those costs are rough ones. The Responses API always sends counts in its last event.

## Use a gateway

If your model calls go through a gateway, such as a LiteLLM proxy, name its host and the format it speaks:

```toml
[agent]
model_hosts = [{host = "llm-gateway.internal", provider = "openai"}]
```

## Tools the model is offered

Derail reads the tools in each model request before it goes out.

| The request offers | What happens |
|---|---|
| a tool Derail can see | it goes ahead |
| a provider tool that only reads, such as `web_search` | it goes ahead, the record notes it, and the table warns, because providers bill those per use and the budget cannot see that |
| a hosted MCP server, which runs at the provider and can change data | the request is not sent unless you list it: `real = ["hosted:mcp/orders"]` |
| a tool of a type Derail does not know | the test stops and names the type |
| your own tool, but no gate sees it | the request is not sent and the test stops, naming the tool to add under `tools`; use `{path, name}` when the model sees another name |

A built-in tool your code runs, such as `shell` or `apply_patch`, has no name of its own, so its type is the name.
List the function that runs it, such as `{path = "package.module:run_shell", name = "shell"}`.

A chat request body Derail cannot read, such as a compressed one, is refused, because its tools cannot be checked.

## Calls that create something at the provider

Chat and Responses calls are model calls. Embeddings, moderations and input-token counts only read or compute, so they run for real under their own labels, such as `model:openai/moderations`; the last two cost no tokens and need no price.

Any other write to a model host, such as starting a fine-tuning job, is refused: it could leave a job or a file behind that nothing cleans up, at a cost the budget cannot see. Reads go through.
