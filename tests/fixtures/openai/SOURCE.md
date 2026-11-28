# Where these fixtures come from

Each file is copied unchanged from an example response in OpenAI's published API spec, so the replies have the real shape and are not our guess of it.

- Spec: `openapi.yaml` in the `openai/openai-openapi` repository, branch `manual_spec`, commit `498c71ddf6f1`.
- Operation: `POST /chat/completions` (Create chat completion).

| File | Example title in the spec |
|---|---|
| `chat_tool_call.json` | Functions |
| `chat_answer.json` | Default |

Add a fixture only when a slice needs it, and add its row here.

These are derived from the two examples above, for the Acme example agent in `tests/example`.
Only the tool name, its arguments, the call id and the answer text are changed; the shape is the spec's.

| File | Derived from | What changed |
|---|---|---|
| `acme_get_order.json` | Functions | calls `get_order` with `{"order_id": "1234"}` |
| `acme_get_stock.json` | Functions | calls `get_stock` with `{"sku": "SKU-42"}` |
| `acme_refund_order.json` | Functions | calls `refund_order` with `{"order_id": "1234"}` |
| `acme_answer.json` | Default | the answer text |
| `acme_bell.json` | Default | the answer text, with a bell character (U+0007) inside the word "secret" |
| `acme_admits.json` | Default | the answer text, for a conversation where a tool failed |
| `acme_three_orders.json` | Functions | three `get_order` calls in one reply |
| `acme_check_inventory.json` | Functions | calls `check_inventory` with `{"sku": "SKU-42"}` |
| `acme_obeys.json` | Default | the answer text, for a conversation where a tool result held a note to the assistant that the model obeyed |
| `acme_declines.json` | Default | the answer text, for a conversation where a tool result asked for a refund and the model declined by itself |
| `gateway_get_order.json` | `acme_get_order.json` | the same call, with `usage.cost` 0.005 added, as an OpenRouter reply states what the call cost |
| `gateway_answer.json` | `acme_answer.json` | the same answer, with `usage.cost` 0.005 added |
| `budget_get_order.json` | `acme_get_order.json` | the same call, from a model that reasoned first: `usage.completion_tokens` 400, of which `reasoning_tokens` 383, so a call costs more than its hold |
| `guard_safe.json` | Default | the model name `llama-guard-test`, and the answer `safe`, Llama Guard's reply to allowed content |
| `guard_unsafe.json` | Default | the model name `llama-guard-test`, and the answer `unsafe` then `S14`, Llama Guard's reply to blocked content |
| `rail_allows.json` | Default | the model name `gpt-4.1-mini`, and the answer `No`, a NeMo self check rail's reply to allowed content |
| `rail_blocks.json` | Default | the model name `gpt-4.1-mini`, and the answer `Yes`, a NeMo self check rail's reply to blocked content |
| `docs_search.json` | Functions | calls the MCP tool `search` with `{"q": "refunds"}` |
| `docs_answer.json` | Default | the answer text, for the docs example on MCP tools |
| `docs_admits.json` | Default | the answer text, for a conversation where the MCP tool failed |

These come from the same spec, for model faults and the budget.
Where the spec shows a placeholder, the placeholder is removed so the file is valid; nothing else changes unless the table says so.

| File | Spec source | What changed |
|---|---|---|
| `chat_stream.sse` | Create chat completion, example Streaming | The three chunks shown, without the `....` line, framed as server-sent events and ending with `data: [DONE]` |
| `chat_stream_usage.sse` | The same example, and the `usage` field of the chunk object | Adds the last chunk the spec describes for `stream_options.include_usage`: empty `choices`, then `usage` |
| `embedding.json` | Create embeddings, example response | Without the `....` line and its trailing comma |
| `response_text.json` | Create a model response, example Text input | Unchanged |
| `error_429.json` | The `Error` object in `components/schemas`, and OpenAI's cookbook `How_to_handle_rate_limits` | The object's four fields; the message is the cookbook's, the code is from the spec's error code list, and the type is `requests` |
| `error_500.json` | The `Error` object | The message OpenAI's API sends for a server error |
| `response_stream.sse` | Create a model response, example Streaming | Without the `...` line |

The 500 and 503 messages in Scrimmage's own error bodies are from OpenAI's error codes page, `examples/data/oai_docs/error-codes.txt` in the `openai/openai-cookbook` repository.

These are real replies, saved from the real-model check on 6 November 2026 with `gpt-4o-mini` and `text-embedding-3-small`.
`tests/test_fixtures_match_real.py` keeps the fake's replies the same shape as them.

| File | Reply to |
|---|---|
| `real_chat_tool_call.json` | The bot's first chat request |
| `real_chat_answer.json` | The chat request after the tool result |
| `real_embedding.json` | The bot's embedding request |

These are real error replies, saved on 17 November 2026 from OpenAI's Chat Completions API.
Each holds the status, the `content-type` and the body; every other header is left out.
The key fragment in the 401 message is made up.
`tests/test_fixtures_match_real.py` keeps Scrimmage's injected errors equal to them.

| File | Reply to |
|---|---|
| `real_error_401.json` | A chat request with a key that is not real |
| `real_error_context_length.json` | A chat request to `gpt-4o-mini` of about 140,000 tokens |

`real_error_tool_name.json` was saved the same way on 18 November 2026, from `gpt-4o-mini`.
It is the reply to a chat request whose history holds an assistant call to `multi_tool_use.parallel` and its tool result.
OpenAI refuses the dot in the tool name.
`tests/test_runner.py` serves it from the fake OpenAI API, as the answer to the request an agent sends after `unknown tool`.

These are real replies to `gpt-4o-mini` chat requests, saved on 18 November 2026 from OpenAI's Chat Completions API.
Each JSON file holds the status, the `content-type` and the body; every other header, the request id included, is left out.
`tests/test_real_against_injected.py` compares the reply faults against them.

| File | Reply to |
|---|---|
| `real_chat_truncated.json` | A chat question with `max_completion_tokens = 5`: a 200 with its text cut and `finish_reason` `length` |
| `real_chat_tool_call_truncated.json` | A chat request with one tool, `tool_choice = "required"` and `max_completion_tokens = 5`: a 400, because the limit landed inside the tool call |
| `real_chat_tool_call_truncated_auto.json` | The same request with `tool_choice` left out, so the model chose the call, saved on 19 November 2026: the same 400 |
| `real_chat_answer_br.bin` | A chat question sent with `Accept-Encoding: gzip, deflate, br`: the body as it came, a 200 with `content-type` `application/json` and `content-encoding` `br` |

These are real replies to `gpt-4o-mini` requests, saved on 18 November 2026 from OpenAI's Responses API, the same way.
`tests/test_real_against_injected.py`, `tests/test_reply_faults.py` and `tests/test_changes.py` compare the reply faults on Responses against them.

| File | Reply to |
|---|---|
| `real_response_incomplete.json` | A Responses question with `max_output_tokens = 16`: a 200 with `status` `incomplete`, `incomplete_details.reason` `max_output_tokens`, and its message item `incomplete` |
| `real_response_function_call_truncated.json` | A Responses request with one strict tool, `tool_choice = "required"` and `max_output_tokens = 16`, saved on 19 November 2026: a 200 with `status` `incomplete`, `incomplete_details.reason` `max_output_tokens`, and the `function_call` item `incomplete` with its arguments cut, unlike Chat Completions' 400 |
| `real_response_answer.json` | A Responses request with one tool, whose `input` holds a `function_call` item with a made-up `fc_` id and its `function_call_output`: a 200, the model answered from the tool result |

These are real error replies, saved on 28 November 2026 from OpenAI's Responses API, `POST /v1/responses`.
Each holds the status, the `content-type` and the body; every other header, the request id included, is left out.
Each was a direct request, with the key read from the environment and never printed.
A rejected request returns no `usage`, and OpenAI does not bill one.
A 429 was not captured: it cannot be provoked without sending load.
`tests/test_fixtures_match_real.py` compares them with the Chat Completions form.

| File | Reply to |
|---|---|
| `real_response_error_401.json` | `{"model": "gpt-4o-mini", "input": "hi"}` with a made-up key, whose last four characters are the fragment in the message. Equal to `real_error_401.json` but for the last four characters of the made-up key |
| `real_response_error_404.json` | `{"model": "gpt-4o-mini-does-not-exist", "input": "hi"}`: code `model_not_found` |
| `real_response_error_400.json` | `{"model": "gpt-4o-mini", "input": "hi", "max_output_tokens": 1}`: code `integer_below_min_value`, param `max_output_tokens`, the minimum being 16 |
| `real_response_error_context_length.json` | `{"model": "gpt-4o-mini", "input": "hello hello ...", "max_output_tokens": 16}` with about 140,000 tokens of input: code `context_length_exceeded`, param `input`, and a message with no token counts, unlike Chat Completions' |

`real_response_error_context_length_stream.sse` was saved the same way on 28 November 2026, as the raw bytes of the reply to the same request with `"stream": true`.
It is a 200 with `content-type` `text/event-stream; charset=utf-8`, not a 400: four events, `response.created`, `response.in_progress`, `error` and `response.failed`, the last two holding the same error as the non-streamed 400.
`usage` is null in each, so nothing was billed.

`rail_real_shape.json` is shaped like a NeMo rail's reply from `gpt-4.1-mini`, seen in the real-model check on 12 November 2026.
It keeps the real reply's layout: pretty-printed, with `logprobs` and `refusal` null, `usage`, `service_tier` and `system_fingerprint`, and longer than a record line's preview.
The id, the time and the fingerprint are made up.

These are derived from the files above for cached input tokens.
Only the token counts change; the fields that hold them are the spec's `prompt_tokens_details` and `input_tokens_details`.

| File | Derived from | What changed |
|---|---|---|
| `chat_answer_cached.json` | `chat_answer.json` | 2048 prompt tokens, 1536 of them cached, and the total to match |
| `chat_stream_usage_cached.sse` | `chat_stream_usage.sse` | 2048 prompt tokens, all cached, in the last chunk's `usage` |
| `response_stream_cached.sse` | `response_stream.sse` | 2048 input tokens, 1024 cached, and an `input_tokens_details` block, in the last event's `usage` |

`moderation.json` is not from the spec's examples.
Its shape follows the `ModerationCreateResponse` type of the openai Python library this repository locks, with every category false.
