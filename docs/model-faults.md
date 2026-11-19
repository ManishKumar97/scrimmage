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

Scrimmage answers the model call with a real HTTP 429, without touching the network.
The openai library reads it exactly as it would read a real one.

Why `first 3`?
The openai library retries a 429 twice by itself before your code sees anything.
Each retry meets the gate again, so three failures are needed to get past the library:

| Attempt | What happens |
|---|---|
| 1 | Scrimmage answers 429; the library waits and retries |
| 2 | Scrimmage answers 429; the library waits and retries |
| 3 | Scrimmage answers 429; the library gives up and raises `RateLimitError` into your code |

With `first 1`, the first retry goes through and your agent never sees an error, which mainly tests the library.

Nothing is sent to the provider for a faulted attempt, so it costs nothing.

## Faults you can use on the model

| Fault | What your agent sees |
|---|---|
| `status 429` | the library's `RateLimitError`, after its retries |
| `status 500` | the library's `InternalServerError`, after its retries |
| `status 400` | the library's `BadRequestError`, at once |
| `status 401` | the library's `AuthenticationError`, at once, with the code `invalid_api_key`: OpenAI's reply to a bad API key |
| `context too long` | the library's `BadRequestError`, at once, with the code `context_length_exceeded`: OpenAI's reply to a prompt over the model's context |
| `timeout` | the library's `APITimeoutError`, after its retries |
| `refused` | the library's `APIConnectionError`, after its retries |
| `reset` | the library's `APIConnectionError`, after its retries |
| `dns` | the library's `APIConnectionError`, after its retries |
| `bad body` | `json.JSONDecodeError`, at once: a complete 200 whose JSON does not parse |

Any status from 400 to 599 works.
`bad body` sends the first 60 bytes of a real reply for the call: an embeddings reply on `model:openai/embeddings`, a Responses reply on `/responses`, and a chat reply on every other call, a moderation or a token count included.
The library does not retry a 200, so `bad body` meets one attempt.
On a streamed request it is a mistake in the test, and the call fails at once: a broken stream is a later fault.
The openai library retries a connection error twice by default, and each attempt meets the fault again, so `when = "first 1"` is absorbed by the first retry.

Two more let the call happen, on a chat call only:

| Fault | What your agent sees |
|---|---|
| `replace`, with `text` and a `model` | the real reply, with the text of each choice replaced by `text`, or each `find` in it |
| `slow 8s` | the real reply, eight seconds late; or the library's own timeout, if the client's is shorter |

Both make the real call, so it is reserved and billed as in the baseline row.
`replace` needs `model`, or it would rewrite every model's answer, the main model's too.
`append` does not work on a model call: append to a tool result the model reads instead, see [Changing a result](changing-results.md).
Neither works on a call that only reads, such as `model:openai/moderations`, whose reply is not text.

## A bad API key, and a prompt that is too long

`status 401` is the auth error.
Its body is copied from a real OpenAI 401, the reply to a request with a key that was not real: the code `invalid_api_key`, and the message `Incorrect API key provided: sk-proj-****...xxxx. You can find your API key at ...`.
The key fragment in the message is made up.
As in the real reply, the content type is `text/plain` while the body is JSON; the openai library reads it either way.

`context too long` is the error OpenAI sends when a chat request is longer than the model's context:

```toml
[[test.fault]]
call = "model"
do   = "context too long"
when = "first 1"
```

The openai library raises `BadRequestError` with `code` `context_length_exceeded` and `param` `messages`, and does not retry it.
An agent that shortens its prompt when it reads that code can be tested this way.
The body is copied from a real reply, so its message names that request's numbers: `This model's maximum context length is 128000 tokens. However, your messages resulted in 140008 tokens.`
They are not your model's context or your prompt's length.
It works on a chat call, `model` or `model:openai`, and not on a call that only reads, such as `model:*/embeddings`.
On a Responses call it is a mistake in the test, and the call fails at once: the Responses API's reply is not this one.

## A reply that comes back wrong

These are the quiet failures: the model answers 200, the time and the cost look normal, and the reply is wrong.
Each fault copies a case seen in the real world:

| Fault | What your agent gets | Where it was seen |
|---|---|---|
| `refuse` | a refusal in place of the answer | OpenAI's Structured Outputs guide, and a forum report of `gpt-4o-mini` refusing in plain text |
| `unknown tool` | a call to `multi_tool_use.parallel`, a tool your agent never offered | OpenAI forum reports from November 2023 to July 2024 |
| `garble` | the real reply with every fifth word replaced by `สวัสดี` | Anthropic's postmortem of August and September 2025: stray Thai or Chinese tokens in English replies |
| `truncate` | the real reply cut short, with `finish_reason` `length`; for a reply of tool calls alone, OpenAI's 400 for a reply cut inside a tool call | real replies cut by `max_completion_tokens` |
| `bad tool args` | the real tool calls with arguments that are not valid JSON | OpenAI's API reference: "the model does not always generate valid JSON" |

Each needs `model`, as `replace` does, so a guard or a judge beside the main model is left alone:

```toml
[[test.fault]]
call   = "model"
model  = "gpt-4o-mini"
do     = "refuse"
when   = "call 2"
expect = [["sorry", "could not"]]
```

They work on a chat call, `model` or `model:openai`, and take no `text`, `find` or `retry_after`.
Under `when = "always"` each needs its own `expect` or `forbid`, as any fault that hides the data does.

What each costs:

| Fault | The real call | Billed |
|---|---|---|
| `refuse`, `unknown tool` | not sent: Scrimmage builds the whole reply | no, and nothing is reserved |
| `garble`, `truncate`, `bad tool args` | sent, then the reply is changed | yes, as in the baseline row, reserved first |

`garble`, `truncate` and `bad tool args` send the real request, so the call is reserved and costs what the baseline's does.
The reply's `usage` stays the model's own counts, so the line's cost is what was billed.
The record keeps both replies: `result` is the real one, and `changed.reply` the one your agent got.
When your agent got another status than the real reply's, as after `truncate` on tool calls alone, `changed.status` holds it.
`scrimmage show` prints them side by side, as `the model said ...; garble gave the agent: ...`.

A built reply is made from a real `gpt-4o-mini` reply: the same fields, with new ids in OpenAI's forms, the time, and the model your request named.
Its `usage` holds a rough count of your request's tokens and 10 output tokens, but nothing was sent, so the line costs 0.

### The two forms of `refuse`

A real refusal has the form your request asked for:

| The request | The reply |
|---|---|
| Structured Outputs: `response_format` of type `json_schema`, as `client.chat.completions.parse()` and LangChain's `with_structured_output` send | `content` null, and `refusal` set to `I'm sorry, I cannot assist with that request.` |
| anything else, JSON mode included | `content` set to `I'm sorry, I can't assist with that.`, and `refusal` null |

`finish_reason` is `stop` either way.
Under Structured Outputs, `parse()` gives `parsed` as `None` with the refusal beside it, and LangChain raises `OpenAIRefusalError`, as they do for a real one.
On a plain request the refusal is ordinary text, so an agent that only checks `refusal` misses it, as it would in production.

### `unknown tool`

The reply is one tool call, as OpenAI's models have sent it:

```json
{"name": "multi_tool_use.parallel", "arguments": "{\"tool_uses\":[{\"recipient_name\":\"functions.get_order\",\"parameters\":{}}]}"}
```

`recipient_name` names the first tool your request offers, and `finish_reason` is `tool_calls`.
A request where the model cannot pick a tool of its own is left as it is: one that offers no tools, sets `tool_choice` to `"none"`, names the tool in `tool_choice`, or offers tools in the old `functions` field only. The call goes on as in the baseline, and the line says why.
If your agent sends the call back in its next request, OpenAI refuses that request with a 400, because the name holds a dot.

### `garble`, `truncate` and `bad tool args`

Each changes the real reply and leaves every other field as the model sent it: `id`, `model`, `usage` and `system_fingerprint`, and the headers but the length and the encoding.

| Fault | The change | Left as it is when |
|---|---|---|
| `garble` | every fifth word of each choice's text becomes `สวัสดี`; a text of fewer than five words has its last word replaced | the reply has no text, such as tool calls alone or a refusal |
| `truncate` | each choice's text is cut at the last space at or before its middle, and its `finish_reason` becomes `length`; a choice cut in its text loses its tool calls, as it never reached them | the reply has no text and no tool call |
| `truncate`, on a reply where a choice holds tool calls alone | the agent gets OpenAI's own 400: `Could not finish the message because max_tokens or model output limit was reached. Please try again with higher max_tokens.` | |
| `bad tool args` | the arguments of every tool call lose their last `}`, so `{"order_id":"1234"}` becomes `{"order_id":"1234"` | the reply has no tool call |

A reply that is left as it is goes to your agent unchanged, and the line says why in `unchanged`.
The fault has not fired, so the run is left out of the row's result.

What your agent's code meets is what it meets on a real reply of that kind:

- After `truncate` on text, `client.chat.completions.parse()` raises `LengthFinishReasonError`.
- After `truncate` on tool calls alone, the openai library raises `BadRequestError`, and does not retry it.
  OpenAI sends no cut-off tool call: when the limit lands inside one, the whole request fails with this 400.
  Two real replies show it, one with `tool_choice` left to the model and one with `tool_choice` `"required"`.
- After `bad tool args`, `json.loads` raises `JSONDecodeError`, and LangChain puts the call in `invalid_tool_calls`.

To `garble`, a word is what lies between spaces or line breaks, and those stay as they were.
Text in a script that puts no spaces between words, such as Thai or Japanese, is one word to it.

A reply in gzip or deflate is unpacked, changed and sent to your agent with no encoding, and so is one in `br` when your program has `brotli` or `brotlicffi` 1.2 or later, see [Limits](limits.md).

### The 400 after a reply fault

A real 400 from the model, on a chat request built from a faulted reply, is your agent's result, not an infrastructure error.
The 400 is what the real misbehaviour leads to.
"Built from it" is read from the request as it is sent: its history holds the faulted reply's tool call id or tool name, a tool result for that call, or the refusal text.
The request also goes to the same model call, such as `model:openai`.
The run is judged as it ended: an agent that does not catch the error crashes, and the row fails.
The 400's line has `after_reply_fault` set to `true`, so the record shows why it was not counted as an infrastructure error.
These stay infrastructure errors, as they say nothing about a request your agent built from the fault:

- any other status, such as a 401, a 429 or a 500;
- a 400 on a request that holds nothing of the faulted reply, such as an embeddings call or a fresh conversation;
- a 400 on a request sent before the fault fired, even when its reply comes back after;
- a 400 after any other fault, such as `timeout` or `status 503`.

Only the process the fault fired in marks its 400s: a 400 sent from a child process the agent started is still an infrastructure error.

### Where they do not work

A streamed request is a mistake in the test, found from the request's `stream`, so the call fails at once and nothing is sent.
A Responses API call is a mistake in the test too: these faults work on Chat Completions only.

## A rate limit that says how long to wait

A rate limiter's 429 often says how long to wait, in a `Retry-After` header.
Add `retry_after` to send one:

```toml
[[test.fault]]
call        = "model"
do          = "status 429"
retry_after = 30
when        = "first 1"
```

The 429 carries `retry-after: 30`, and nothing is sent to the provider.
`retry_after` works with `status 429` and `status 503`, in whole seconds from 0.
It must be under `run_timeout`, or the test stops with a mistake: `retry_after 130 is not under run_timeout, 120`.

The openai library reads the header before it retries:

| `retry_after` | What the openai library does |
|---|---|
| 1 to 120 | waits that many seconds, then retries |
| over 120 | does not retry: your code gets `RateLimitError` after one attempt |
| 0 | waits its own short backoff, as with no header |

The wait comes before each retry.
With `when = "always"` and the library's default of two retries, it waits twice: `retry_after = 60` waits 120 seconds, which can reach `run_timeout`.
The wait is real, so it counts toward `run_timeout`.
Scrimmage sends the header in seconds only: not as a date, and not as openai's own `retry-after-ms`.

## A guard model

A guard model, such as Llama Guard, is a model call like any other; name it by its model name.
If it runs on a host of its own, name that host under `model_hosts`, so its calls are model calls: budgeted, and faulted by name.

```toml
[agent]
function    = "shop.agent:answer"
model_hosts = [{host = "guard.internal", provider = "openai"}]

[agent.prices]
"meta-llama/Llama-Guard-4-12B" = {input = 0, output = 0}  # self-hosted: no bill

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped"]

[[test.fault]]
call   = "model"
model  = "meta-llama/Llama-Guard-4-12B"
do     = "replace"
text   = "safe"            # the whole reply text becomes this
expect = ["Request blocked by policy RB7"]

[[test.fault]]
call   = "model"
model  = "meta-llama/Llama-Guard-4-12B"
do     = "slow 8s"
expect = ["Sorry, the safety check is unavailable"]
```

How `replace` meets a reply:

| The reply | What happens |
|---|---|
| a chat completion with text | its text is changed; the line keeps the real reply, and the injected text beside it |
| tool calls and no text, a refusal with no text, a `find` that is not in it, a status other than 200 | the agent gets it as it is; the call is not counted as fired |
| a streamed reply | a mistake in the test: turn streaming off for that model, or fault it another way |
| a Responses API call | a mistake in the test: `replace` works on Chat Completions only |

A call left as it is still uses its number in `first N`: under `first 1`, a first reply with only tool calls means no call of that run is changed.
`slow` waits before it reserves, so a slowed call holds no budget while it waits.
If the request's read timeout is shorter than the delay, the call ends at that timeout with the library's own error, and nothing is sent or billed.
The openai library retries a timeout twice by default, so under `when = "always"` a guard behind it waits about three times the client timeout before the agent sees an error.
See [Testing your guardrails](guardrails.md) for the rows that use these.

## Choosing when it fires

| `when` | Fires on | Use it for |
|---|---|---|
| `always` (default) | every matching call | an outage that never ends: does the agent admit it? |
| `first N` | the first N attempts, retries included | an outage that ends: does the agent recover? |
| `requests N` | the first N requests, and every retry of each | failing a whole request however many retries the library makes |
| `call N` | the Nth attempt alone, retries included in the count | one failure in the middle of a run: does the agent carry on? |

`first 3` depends on the library retrying exactly twice.
`call N` counts attempts as `first N` does, so on a model call the library's retry is the next call and usually absorbs the fault: your agent sees no error.
To fail a whole request, retries and all, use `requests N`.
If your client sets its own `max_retries`, use `requests 1`: it fails the request and all its retries, whatever their number.
`requests N` needs a library that marks its retries, as the openai library does with its `x-stainless-retry-count` header.
On a client that marks none, such as NeMo Guardrails' own, each attempt would count as a request, so the run gets a mistake that says to use `first N` or `always`.

Calls are counted per run.
When two model calls run at once, such as a guardrail next to the main call, Scrimmage matches each retry to its request by the thread or task that sent it and the request's content, so each request keeps its own number.

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
| `gpt-4.1`, attempt 1 | Scrimmage answers 429; the library retries |
| `gpt-4.1`, attempt 2 | Scrimmage answers 429; the library retries |
| `gpt-4.1`, attempt 3 | Scrimmage answers 429; the library raises `RateLimitError` into your code |

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
A gateway's alias works the same way: Scrimmage sits in front of the gateway, so it sees the alias and never the name behind it.

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

### A request with no name Scrimmage can read

Scrimmage reads the name from the `model` field of the request body.
That reading changes nothing: the request goes out byte for byte as your code built it, so the provider's prompt cache is not disturbed.

When a fault gives `model` and a call its label names has no such field, Scrimmage cannot tell whether the fault is for that call.
So the call is refused before it is sent, with the reason `unreadable_model`, and the test stops with exit code 2.
That covers a request body sent from a generator, a multipart or compressed body, and a body with no `model` at all, such as a Responses call on a stored prompt.
Firing could hit the wrong call, and letting it through could spare the right one.

For a stored prompt, send `model` on that call.
The openai library takes `model` beside `prompt`, but Scrimmage has not confirmed what the provider does when the stored prompt names another model.

Taking `model` off the fault is the other way out, and it has two costs.
The fault hits every call its label names again, such as the router.
And with a budget on, any model call with no name, such as a Responses call on a stored prompt, has no price, so it is refused in the baseline too and the fault row never runs.
Give calls with no name a price, as the key `""` under `[agent.prices]`, or set `max_cost_usd = "none"`.

Calls the fault's label does not name go on as usual, and so does everything in a test with no `model` on its fault.

## Keep a budget

Every test has a budget for the whole run of `scrimmage test`. It is $5 unless you change it:

```toml
[agent]
max_cost_usd = 0.50
```

Before each model call, Scrimmage reserves what the call could cost: an estimate of its input at the full input price, plus its `max_tokens` (or `reserve_output_tokens`, 4096 by default) at the output price.
After the call, the real cost from the reply replaces the reservation.
Scrimmage reads it from the reply's usage, after undoing `gzip`, `deflate` or `br`; for `br` it uses your program's own `brotli` or `brotlicffi` 1.2 or later, which is also what makes httpx ask for `br`.
So four calls at once cannot all slip past the limit.
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

### Prices

Scrimmage ships prices for common OpenAI models, cached input included, checked against OpenAI's pricing page on 7 November 2026.
A dated model, such as `gpt-4o-mini-2024-07-18`, uses its family's price.
For any other model, give the price in dollars per million tokens:

```toml
[agent.prices]
llama-70b = {input = 0.6, output = 0.6}
acme-gpt = {input = 2.0, cached_input = 0.5, output = 8.0}
```

### Cached input

OpenAI caches a prompt over 1024 tokens that repeats, and bills the cached part at a lower price.
Scrimmage reads the cached count from each reply's usage: `prompt_tokens_details.cached_tokens` on Chat Completions, `input_tokens_details.cached_tokens` on Responses, streamed or not.
The record line shows it as `tokens.cached`, and `cost_usd` uses it, so the cost column matches the bill.

| Reply | Price of each input token |
|---|---|
| 2048 input tokens, 1536 of them cached, on `gpt-4o-mini` | 512 at $0.15 and 1536 at $0.075 per million |
| a price under `[agent.prices]` with no `cached_input` | every input token at `input`, the safe overestimate |
| a cached count above the input count, or not a whole number | every input token at `input` |

The reservation before a call still counts every input token at the full price, since Scrimmage cannot know what the provider will serve from its cache.

A call that sends no model name, such as a Responses call on a stored prompt, is priced by the key `""`:

```toml
[agent.prices]
"" = {input = 2.0, output = 8.0}
```

With a budget on, a model with no price stops the test at its first call, because it cannot be reserved for.
With `max_cost_usd = "none"`, no price is needed, and tokens are still counted.

A reply without usable token counts gets a rough cost, worked out from how many bytes went each way, and its record line has `cost_estimated` set.
On Chat Completions a stream only carries counts if your request sets `stream_options.include_usage`. Scrimmage leaves your request as it is, so without that setting those costs are rough ones. The Responses API always sends counts in its last event.

## Use a gateway

If your model calls go through a gateway, such as a LiteLLM proxy, name its host and the format it speaks:

```toml
[agent]
model_hosts = [{host = "llm-gateway.internal", provider = "openai"}]
```

## Tools the model is offered

Scrimmage reads the tools in each model request before it goes out.

| The request offers | What happens |
|---|---|
| a tool Scrimmage can see | it goes ahead |
| a provider tool that only reads, such as `web_search` | it goes ahead, the record notes it, and the table warns, because providers bill those per use and the budget cannot see that |
| a hosted MCP server, which runs at the provider and can change data | the request is not sent unless you list it: `real = ["hosted:mcp/orders"]` |
| a tool of a type Scrimmage does not know | the test stops and names the type |
| your own tool, but no gate sees it | the request is not sent and the test stops, naming the tool to add under `tools`; use `{path, name}` when the model sees another name |

A built-in tool your code runs, such as `shell` or `apply_patch`, has no name of its own, so its type is the name.
List the function that runs it, such as `{path = "package.module:run_shell", name = "shell"}`.

A chat request body Scrimmage cannot read, such as a compressed one, is refused, because its tools cannot be checked.

## Calls that create something at the provider

Chat and Responses calls are model calls. Embeddings, moderations and input-token counts only read or compute, so they run for real under their own labels, such as `model:openai/moderations`; the last two cost no tokens and need no price.

Any other write to a model host, such as starting a fine-tuning job, is refused: it could leave a job or a file behind that nothing cleans up, at a cost the budget cannot see. Reads go through.
