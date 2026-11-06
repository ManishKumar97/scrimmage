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

Faultline answers the model call with a real HTTP 429, without touching the network.
The openai library reads it exactly as it would read a real one.

Why `first 3`?
The openai library retries a 429 twice by itself before your code sees anything.
Each retry meets the gate again, so three failures are needed to get past the library:

| Attempt | What happens |
|---|---|
| 1 | Faultline answers 429; the library waits and retries |
| 2 | Faultline answers 429; the library waits and retries |
| 3 | Faultline answers 429; the library gives up and raises `RateLimitError` into your code |

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
When two model calls run at once, such as a guardrail next to the main call, Faultline matches each retry to its request by the thread or task that sent it and the request's content, so each request keeps its own number.

## Which model calls a label names

| Label | Means |
|---|---|
| `model` | every chat call, on any provider |
| `model:openai` | chat calls to OpenAI |
| `model:openai/embeddings` | embedding calls to OpenAI |
| `model:*/embeddings` | embedding calls on any provider |

A fault on `model` leaves embeddings alone, so you can fail the chat and keep your retrieval working, or the other way around.

Model calls never need listing under `real`, and they cannot be mocked: the test exists to see how the real model reacts.

## Keep a budget

Every test has a budget for the whole run of `faultline test`. It is $5 unless you change it:

```toml
[agent]
max_cost_usd = 0.50
```

Before each model call, Faultline reserves what the call could cost: an estimate of its input, plus its `max_tokens` (or `reserve_output_tokens`, 4096 by default) at the output price.
After the call, the real cost from the reply replaces the reservation.
So four calls at once cannot all slip past the limit.

When the budget cannot cover a call, the call is not made, that run ends as "budget reached", and the test stops with exit code 2.
The table's last line says what the test spent.

To turn the budget off on purpose:

```toml
max_cost_usd = "none"
```

### Prices

Faultline ships prices for common OpenAI models.
A dated model, such as `gpt-4o-mini-2024-07-18`, uses its family's price.
For any other model, give the price in dollars per million tokens:

```toml
[agent.prices]
llama-70b = {input = 0.6, output = 0.6}
```

With a budget on, a model with no price stops the test at its first call, because it cannot be reserved for.
With `max_cost_usd = "none"`, no price is needed, and tokens are still counted.

A reply without usable token counts gets a rough cost, worked out from how many bytes went each way, and its record line has `cost_estimated` set.
On Chat Completions a stream only carries counts if your request sets `stream_options.include_usage`. Faultline leaves your request as it is, so without that setting those costs are rough ones. The Responses API always sends counts in its last event.

## Use a gateway

If your model calls go through a gateway, such as a LiteLLM proxy, name its host and the format it speaks:

```toml
[agent]
model_hosts = [{host = "llm-gateway.internal", provider = "openai"}]
```

## Tools the model is offered

Faultline reads the tools in each model request before it goes out.

| The request offers | What happens |
|---|---|
| a tool Faultline can see | it goes ahead |
| a provider tool that only reads, such as `web_search` | it goes ahead, the record notes it, and the table warns, because providers bill those per use and the budget cannot see that |
| a hosted MCP server, which runs at the provider and can change data | the request is not sent unless you list it: `real = ["hosted:mcp/orders"]` |
| a tool of a type Faultline does not know | the test stops and names the type |
| your own tool, but no gate sees it | the request is not sent and the test stops, naming the tool to add under `tools`; use `{path, name}` when the model sees another name |

A request body Faultline cannot read, such as a compressed one, is refused, because its tools cannot be checked.

## Calls that create something at the provider

Chat and Responses calls are model calls. Embeddings, moderations and input-token counts only read or compute, so they run for real under their own labels, such as `model:openai/moderations`; the last two cost no tokens and need no price.

Any other write to a model host, such as starting a fine-tuning job, is refused: it could leave a job or a file behind that nothing cleans up, at a cost the budget cannot see. Reads go through.
