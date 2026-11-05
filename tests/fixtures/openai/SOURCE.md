# Where these fixtures come from

Each file is copied unchanged from an example response in OpenAI's published API spec, so the replies have the real shape and are not our guess of it.

- Spec: `openapi.yaml` in the `openai/openai-openapi` repository, branch `manual_spec`, commit `498c71ddf6f1`.
- Operation: `POST /chat/completions` (Create chat completion).

| File | Example title in the spec |
|---|---|
| `chat_tool_call.json` | Functions |
| `chat_answer.json` | Default |

Add a fixture only when a slice needs it, and add its row here.
