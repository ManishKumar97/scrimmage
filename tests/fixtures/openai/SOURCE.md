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
| `acme_admits.json` | Default | the answer text, for a conversation where a tool failed |
| `acme_three_orders.json` | Functions | three `get_order` calls in one reply |
| `acme_check_inventory.json` | Functions | calls `check_inventory` with `{"sku": "SKU-42"}` |
| `acme_obeys.json` | Default | the answer text, for a conversation where a tool result held a note to the assistant that the model obeyed |
| `acme_declines.json` | Default | the answer text, for a conversation where a tool result asked for a refund and the model declined by itself |
| `guard_safe.json` | Default | the model name `llama-guard-test`, and the answer `safe`, Llama Guard's reply to allowed content |
| `guard_unsafe.json` | Default | the model name `llama-guard-test`, and the answer `unsafe` then `S14`, Llama Guard's reply to blocked content |
| `rail_allows.json` | Default | the model name `gpt-4.1-nano`, and the answer `No`, a NeMo self check rail's reply to allowed content |
| `rail_blocks.json` | Default | the model name `gpt-4.1-nano`, and the answer `Yes`, a NeMo self check rail's reply to blocked content |
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

The 401, 500 and 503 messages in Scrimmage's own error bodies are from OpenAI's error codes page, `examples/data/oai_docs/error-codes.txt` in the `openai/openai-cookbook` repository.

These are real replies, saved from the real-model check on 6 November 2026 with `gpt-4o-mini` and `text-embedding-3-small`.
`tests/test_fixtures_match_real.py` keeps the fake's replies the same shape as them.

| File | Reply to |
|---|---|
| `real_chat_tool_call.json` | The bot's first chat request |
| `real_chat_answer.json` | The chat request after the tool result |
| `real_embedding.json` | The bot's embedding request |

These are derived from the files above for cached input tokens.
Only the token counts change; the fields that hold them are the spec's `prompt_tokens_details` and `input_tokens_details`.

| File | Derived from | What changed |
|---|---|---|
| `chat_answer_cached.json` | `chat_answer.json` | 2048 prompt tokens, 1536 of them cached, and the total to match |
| `chat_stream_usage_cached.sse` | `chat_stream_usage.sse` | 2048 prompt tokens, all cached, in the last chunk's `usage` |
| `response_stream_cached.sse` | `response_stream.sse` | 2048 input tokens, 1024 cached, and an `input_tokens_details` block, in the last event's `usage` |

`moderation.json` is not from the spec's examples.
Its shape follows the `ModerationCreateResponse` type of the openai Python library this repository locks, with every category false.
