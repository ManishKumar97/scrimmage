# Where these fixtures come from

`tests/test_fixtures_source.py` checks every file here against the sha256 line below, so a file edited by hand fails it.
Add a fixture only when a test needs it, and add its row here.

## Real captures

`real_error_401.json` is a real reply from `POST https://api.anthropic.com/v1/messages`, saved on 23 November 2026.
The request used a key that is not real, so it cost nothing.
The file holds the status, the `content-type` and the body; every other header is left out.

| File | Reply to | sha256 |
|---|---|---|
| `real_error_401.json` | A Messages request for `claude-haiku-5-5` with a key that is not real | `5b08bb13dbd22aeeebd8f6bd89fae90878c0aba97ead6ae7cf801a89b6c9bd94` |

Spend on real calls: $0.00.

## Captures not made

The `ANTHROPIC_API_KEY` in the dev `.env` was rejected by Anthropic on 23 November 2026, with the same 401 as above.
So no real model reply could be saved.
These captures are still to do, each with a valid key, as `real_*` files with their own rows and sha256 lines:

- a plain answer;
- a `tool_use`;
- a streamed `tool_use`;
- a call with a 5 minute and a 1 hour cache write;
- a `tool_use` cut by `max_tokens`;
- the 400 for a prompt over the context window;
- a plain-text decline to a mildly off-limits request;
- a `stop_reason: "refusal"` reply, if it can be got cheaply.

Until then the tests use the documented examples below.
They are not captures, and nothing here may be called real.
The message of the context-length 400 in `provider_anthropic.py` is the one Anthropic's clients report, from memory, and is to be checked against a capture.

## Documented examples

Each file is copied from an example in Anthropic's published docs at `platform.claude.com`, read on 23 November 2026, so the replies have the documented shape and are not our guess of it.
The models named in them are the docs' own.

| File | Docs page and example | What changed | sha256 |
|---|---|---|---|
| `doc_answer.json` | Messages examples, "Hello, Claude" | Unchanged | `902730a6e43d9bfe68cd886e17b34fc7e0cb3dfd3785f49191aab3e0f785b0fd` |
| `doc_tool_use.json` | Handle tool calls, "Example API response with a `tool_use` content block" | Unchanged; the page shows no `usage` | `46aaca3fa3adf87588c86e187847e24a8f78456cd8b141d0f2866e438a6574fa` |
| `doc_stream_answer.sse` | Streaming, "Streaming request" | Unchanged | `bee1873e5b02de52e95ae9dd226868cc8aed2680ec6c84572c3fa49926436c3e` |
| `doc_stream_tool_use.sse` | Streaming, "Streaming request with tool use" | Unchanged | `08451c8c92b1f6e316efead626151b817d7f6cef67c2ad982de9acd6a5afdea5` |
| `doc_refusal.json` | Refusals and fallback, "What a refusal looks like" | Unchanged | `ac0daeae51d3e266fe595cb319339b91ed83bc843de3423891f8bffd1f5dadde` |

These are derived from the files above for cache counts.
Only the `usage` changes.
The cache counts are the ones in the prompt caching page's "Output" example, which has `cache_creation` of 148 tokens for 5 minutes and 100 for 1 hour.

| File | Derived from | What changed | sha256 |
|---|---|---|---|
| `doc_cached.json` | `doc_answer.json` | `usage` is the caching page's: 2048 input, 1800 read, 248 written, 148 for 5 minutes and 100 for 1 hour, 503 output | `75804c2a1077df1c2300eb9aa1daed22db0639b142edeaf2870ded0e0b7bf176` |
| `doc_stream_cached.sse` | `doc_stream_answer.sse` | `message_start` carries those counts, and `message_delta` carries them as `null` with 503 output, as the docs say its other fields may | `9325eaa7b6adb599ee44b50414466323726f497c580d441c4398534972ea53ff` |
