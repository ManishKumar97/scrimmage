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
