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
| `acme_three_orders.json` | Functions | three `get_order` calls in one reply |
| `docs_search.json` | Functions | calls the MCP tool `search` with `{"q": "refunds"}` |

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

The 401, 500 and 503 messages in Derail's own error bodies are from OpenAI's error codes page, `examples/data/oai_docs/error-codes.txt` in the `openai/openai-cookbook` repository.

These are real replies, saved from the real-model check on 6 November 2026 with `gpt-4o-mini` and `text-embedding-3-small`.
`tests/test_fixtures_match_real.py` keeps the fake's replies the same shape as them.

| File | Reply to |
|---|---|
| `real_chat_tool_call.json` | The bot's first chat request |
| `real_chat_answer.json` | The chat request after the tool result |
| `real_embedding.json` | The bot's embedding request |
