# Limits: faults and model calls

Limits of the faults on a model call, of the providers Scrimmage reads, and of the replies it builds.


- **A provider that is neither OpenAI nor Anthropic.**
  Listing its host under `real` lets the calls through, but they are web requests, so the budget does not see them.
  Name a host that speaks OpenAI's or Anthropic's format under `model_hosts` instead.

- **A few faults.** `timeout`, `error` and `empty` on tools, `timeout`, `status N`, `refused`, `reset`, `dns` and `bad body` on model calls and web requests, `context too long` on a chat model call, `empty` on web requests, `timeout`, `error` and `empty` on MCP tools, `append`, `replace` and `slow` on tools, MCP tools and web requests, and `replace`, `slow`, `refuse`, `unknown tool`, `garble`, `truncate` and `bad tool args` on a chat model call.
  `append` on a model call comes later.

- **`empty` needs a declared type with an empty value.** On a tool declared to return a model, a dataclass, a number, `bytes`, a `Sequence`, a `Mapping`, a union of two kinds such as `str | list[str]`, or a type that cannot be resolved when it runs, or a LangChain tool with `response_format = "content_and_artifact"`, `empty` is a test mistake, and the tool never runs. Mock an empty result instead. See [Faults](../concepts.md#faults).

- **Connection faults on a tool or an MCP tool.** `refused`, `reset` and `dns` work where Scrimmage sees the connection, on a model call or a web request. On a tool, write `do = "error"` with `raises = "ConnectionRefusedError"`, `"ConnectionResetError"` or `"OSError"`.

- **An injected `reset` never reaches the server.** The gate raises it before the request is sent. A reset after the server has read the request and done the work, with the reply lost, which tests duplicate writes on retry, is a later fault.

- **A connection fault's cause chain, in detail.** The chain has a real failure's shape: the library's error is raised from httpcore's, which holds the `OSError` as its hidden context, and on the async path anyio's link sits between them. Two details differ. On an async refused connection the `ConnectionRefusedError` at the bottom reads `Connection refused`, where a real one says `Connect call failed` and the address. On an async reset, anyio's `BrokenResourceError` lacks the `IndexError` a real one holds behind its cause. A client on a transport of its own, which never loads httpcore, gets the `OSError` as the direct cause.

- **The `dns` message is Linux's.** The gate writes glibc's text, `[Errno -2] Name or service not known`. On macOS a real failed lookup reads differently; the class, `socket.gaierror`, and the errno, `socket.EAI_NONAME` on that platform, still match.

- **A transport's own retries.** An httpx transport built with `retries=N`, such as `httpx.HTTPTransport(retries=2)`, retries a real failed connect below the gate, so it never sees an injected one. Under `when = "always"` the result is the same; under `first 1` a real refused connection can recover inside the transport, while the injected one reaches your code. Scrimmage does not detect the setting.

- **Only text and plain data take `append` and `replace`.** A result that is a model object, a dataclass, a number, `None`, `bytes` or a `str` subclass is not changed: the call is left as it is, the line says why, and a row with no place for the change is a test mistake. A dict, list or tuple takes only `replace` with `find`, on string values. See [Changing a result](../changing-results.md#what-each-kind-of-call-can-take).

- **An MCP result with several text items.** `append` goes on the last one, and an agent that reads only `content[0].text` never sees it. With structured content, `replace` with `find` must change both the text and the structured content to count.

- **Web bodies that are not text or JSON.** A reply that is a stream (`text/event-stream`, NDJSON), has another content type, is encoded with `zstd`, or is over 10 MB is not changed. A digest header, such as `Content-MD5`, no longer matches a changed body. Clients other than httpx and httpx2, such as `requests`, `aiohttp` and `urllib`, are not seen, so the fault never fires.
  A model reply encoded with `zstd` is not changed either, and its cost is estimated from its size.
  A `br` reply is read with `brotli` or `brotlicffi` 1.2 or later from your program's environment; httpx and httpx2 ask for `br` only when one of them is installed.
  Without one, or with an older one, a `br` reply is not changed, the line says why, and a model reply's cost is estimated from its size.
  `brotlipy`, which also installs as `brotli`, counts as none: it cannot stop at a size, so Scrimmage leaves `br` alone while httpx still unpacks it for your program.
  A packed reply is unpacked to 10 MB at most for the record, so a larger one's `bytes` on the line shows that cap, not its full size.

- **`slow` outside asyncio.** A `slow` fault on an async tool or web request under trio needs an asyncio loop to wait on, so the call is not slowed and the line says so. An MCP call waits with anyio, which works on both.

- **A sync caller that gives up leaves its thread behind.** Under `slow`, a caller that stops waiting with `future.result(timeout=...)` does not stop the thread that waits. That thread runs the real call after the delay, so a caller that retries runs a listed real write twice. Python cannot stop another thread safely.

- **`slow` is not a late reply after the work.** The delay comes before the call. A call that does its work and replies late, which tests duplicate writes on retry, is a later fault. Nor is the delay drawn from a range.

- **The plan holds `find` as written.** The plan is how the program applies a fault, so a `find` cannot be redacted there or it would no longer match. It is held in memory by the runner and served to the program over the loopback control server, and is not written to the records folder; the results and the record line show `find` redacted. It comes from your own `scrimmage.toml`.

- **`${` in an injected text.** `${NAME}` is filled in from the environment anywhere in the file, `text` and `find` included, so an injection that needs a literal `${`, such as a Log4Shell-shaped text, writes it as `$${`.

- **A change over text the record removes is not made.** A `find` that touches a key, or an `append` that joins a key to the text before it, leaves the call unchanged and the line names the rule.

- **`replace` on a model reply is for text replies.** It changes the text of an OpenAI Chat Completions reply and of each `text` block of an Anthropic Messages reply. A streamed reply and an OpenAI Responses API call are a test mistake, a reply with no text is left as it is, and a moderation or embedding result cannot take it.
  The reply is written back as compact JSON, so its bytes differ from the provider's even where its fields do not.

- **`bad body` on a streamed model request.** It is a test mistake: the call fails at once and the fault is not counted as fired. A stream cut midway is a later fault. Fault a request that is not streamed, or use `timeout` or a status on this one.

- **`context too long` is a saved real reply, not your model's.**
  On OpenAI's Chat Completions it is OpenAI's 400 with the code `context_length_exceeded`.
  On an Anthropic call it is Anthropic's 400 with the type `invalid_request_error`.
  On a Responses call it is a test mistake: the call fails at once and the fault is not counted as fired.
  The message names the numbers of the real reply it was saved from, not your model's context or your prompt's length.

- **The faults in a model's reply are not for a streamed request.** `refuse`, `unknown tool`, `garble`, `truncate` and `bad tool args` on a streamed request are a test mistake: the call fails at once and nothing is sent.
  A changed stream is a later fault.
  `replace` is the one that lets the real stream through, with the mistake on the line, because it is a carried fault, which is decided once the reply is back.
  `bad body` on a streamed request is a mistake too.
  What reaches an agent that streams is `timeout`, `refused`, `reset`, `dns`, `status`, `context too long` and `slow`, as for any call.

- **A Responses agent that chains to a faulted reply is not supported.** A request whose `previous_response_id`, or an `item_reference` in its `input`, names a reply or an item a fault changed or built is a test mistake, and it is not sent: OpenAI holds nothing under those made-up ids. Send the conversation in `input`, as the OpenAI Agents SDK does by default.
  A reply fault on a request that names a `conversation` is a test mistake too, and nothing is sent: OpenAI would keep the real reply in that conversation, or none.

- **`truncate` on Responses keeps the real counts.** The reply's `usage.output_tokens` is what the model was billed for, not the request's `max_output_tokens`. With no text, the last `function_call` item is cut, as a real reply cut inside a call is.

- **A 400 after a reply fault on Responses is an infrastructure error.** OpenAI takes back a Responses message or function call under an id it never made and reads it as sent, as two real requests showed, so no 400 is known to follow a fault there, and none is marked `after_reply_fault`.
  The one exception is `unknown tool`: a request that carries the made-up `call_id` is marked, as on Chat Completions, because that call is the failure under test.

- **A built Responses reply's other fields are fixed.** After `refuse`, the reply repeats the fields your request set, such as `instructions`, `store`, `temperature`, `tool_choice` and `text`. Its `tools` is empty whatever your request sent, and `model` is the name your request sent.

- **A built reply's counts are an estimate.** `refuse` and `unknown tool` send nothing, so the reply's `usage` holds a rough count of the request and 10 output tokens, and its `system_fingerprint` is the one of the real reply it was made from. The line costs 0.
  Its `model` is the name your request sent, such as `gpt-4o-mini`, where a real reply names the snapshot, such as `gpt-4o-mini-2024-07-18`.
  It has one choice, whatever `n` your request asks for.

- **A changed reply keeps the real counts.** After `garble`, `truncate` or `bad tool args`, the reply's `usage` is what the model was billed for, so the line's cost is true. A cut reply's `completion_tokens` is then more than its text holds, and an agent that compares it with its own `max_completion_tokens` sees a reply that was not cut by the limit.
  The changed reply is written back as compact JSON, so its bytes differ from the provider's even where its fields do not.

- **`truncate` on a very short text.** A text of one character cannot be cut shorter, so the agent gets the same text with `finish_reason` `length`, and the fault counts as fired.

- **`garble` is one form of corrupted output.** It puts the stray token of one documented incident in every fifth word. Scrambled letters, other scripts and broken code are not built. A text in a script with no spaces between words is one word, so it changes once.

- **`bad tool args` breaks the JSON only.** Arguments that parse but break the tool's schema, such as a missing required field, are not built.

- **The Structured Outputs refusal is the guide's example.** No real refusal was captured: the `refusal` text and form come from OpenAI's Structured Outputs guide, and the plain one from a forum report.

- **`unknown tool` calls with no arguments.** The made-up call has an empty input, `{}` on Anthropic and `"{}"` on OpenAI, whatever the tool; the row cannot set it.
  The default name, an offered tool's name with `_v2` added, is an arbitrary name close to an offered one that the request does not offer, not a name any model sent; name the tool with `tool` when the name matters.
  The run remembers the tool names of each model request by call label and conversation, where a conversation is the system prompt or instructions plus the first user message, so a tool dropped on one provider's call is not remembered for another's.
  Agents are told apart only when their system prompt, instructions, stored prompt or first user message differ; two agents that share all of them are one conversation, and a tool of one can be named in the other's reply.
  A label keeps its newest 64 conversations.
  A Responses request that continues by `previous_response_id` or a `conversation` carries neither, so it has no dropped tool and gets the `_v2` name.
  A multi-agent run whose agents are not told apart that way, or that wants a specific tool, sets `tool`.
  It fires where the model picks its own tool: not with `tool_choice` `"none"`, a `tool_choice` that names a tool, or tools in the old `functions` field only; those calls are left as they are. It still fires with `parallel_tool_calls` false, as no source shows what a model does then.
  If your agent sends that call back, OpenAI's real 400 on the next request is judged as the run's outcome, and its line is marked `after_reply_fault`; see [The 400 after a reply fault](../model-faults.md#the-400-after-a-reply-fault).

- **A body cut by a dropped connection.** `bad body` sends a whole reply whose JSON does not parse. A reply cut short because the connection dropped is a connection error, not a body to parse: the openai library raises `APIConnectionError` from httpx2's `RemoteProtocolError`, after its retries. Scrimmage does not inject that failure.

- **`requests N` needs a library that marks its retries.** It counts a retry by the openai library's `x-stainless-retry-count` header.
  A client that sends none, such as NeMo Guardrails' own, makes each attempt a request; the run then gets a mistake that says to use `first N` or `always`; `call N` counts attempts too, so it works there.

- **A model's path on a host under `real`.** A POST to `/chat/completions` on a host listed under `real` as a web request runs unbudgeted and cannot be faulted by model name; the run table notes it.
  A web API whose path ends the same way gets the note too, and still runs.

- **Guardrails.** Two rails on one model are faulted together, one model name on two hosts cannot be faulted apart, a fallback guard under an attack needs two faults in one run, and a guard called through `requests`, `aiohttp` or `urllib` is not gated.
  See [Testing your guardrails](../guardrails.md#what-is-not-covered).

- **NeMo Guardrails' usage statistics.** NeMo sends them through `urllib`, which Scrimmage does not gate, unless `NEMO_GUARDRAILS_NO_USAGE_STATS=1` is set.

- **Two calls to one model.** A fault's `model` tells models apart, not callers. A planner and a main model that both use `gpt-4.1` are hit together, and `first N` counts them together. Give them different names through a gateway alias if you need to fail one. See [Failing one model](../model-faults.md#failing-one-model).

- **A model name is compared whole.** No prefix, wildcard or family of dated names: `gpt-4o-mini` does not name `gpt-4o-mini-2024-07-18`. If no call sent the exact name, the fault never fires, so the test stops with exit code 2 and lists the names the calls used. If your code sends both `gpt-4o-mini` and `gpt-4o-mini-2024-07-18`, a fault on the first fires on it alone, the dated call runs for real, the row can pass, and nothing warns. Write the name your code sends, not the name a reply or a dashboard shows.

- **A fault with `model` needs a name it can read.** A call its label names is refused and stops the test if its request body is sent from a generator, is multipart or compressed, or has no `model` field, such as a Responses call on a stored prompt. For a stored prompt, send `model` on that call: the openai library takes it beside `prompt`, but what the provider does when the stored prompt names another model is not confirmed. Taking `model` off the fault hits the router again, and with a budget on any model call with no name, such as a Responses call on a stored prompt, is refused for having no price, so give it one as `""` under `[agent.prices]`, or set `max_cost_usd = "none"`. See [Failing one model](../model-faults.md#a-request-with-no-name-scrimmage-can-read).

- **Two `model` keys in one body.** The last value wins when Scrimmage reads it, and a provider might choose differently.

- **`refuse` with `form = "classifier"` names no category.**
  Its `stop_details` has `category` and `explanation` set to `null`, which the anthropic library reads as a refusal that maps to no named category.
  An agent that branches on a category, such as `cyber` or `bio`, is not reached by this fault.
  No real classifier refusal could be captured with a harmless request, so none is copied.

- **A plain `refuse` on a forced tool call or JSON is a mistake in the test.**
  A request with `tool_choice` of type `any` or `tool`, or with `output_config.format` of type `json_schema`, is answered by a real model with the tool call or the JSON, not with words.
  The plain form stops at the call, nothing is sent, and the message says to write `form = "classifier"` on the row, which builds for any request.
  OpenAI's `refuse` has the same gap on a forced `tool_choice` and builds a text refusal anyway.
