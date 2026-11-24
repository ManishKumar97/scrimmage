# Where these fixtures come from

`tests/test_fixtures_source.py` checks every file here against the sha256 line below, so a file edited by hand fails it.
Add a fixture only when a test needs it, and add its row here.

## Real captures

These are real replies from api.anthropic.com, saved on 23 November 2026 (`real_decline_address.json` on 24 November) with the dev key, through `claude-haiku-5-5`, the cheapest current model ($0.10 for a million input tokens and $0.50 for a million output tokens), except the context-length 400, which needs a model with a window the test can pass.
A reply that succeeded is saved as the bytes that came, so its key order, its spaces and its fields are Anthropic's own.
An error is saved as its status, its `content-type` and its body, as the other real errors are.
No other header is kept.

Scrubbed: the `signature` of each `thinking` block and each `encrypted_content` and `encrypted_index` of a search result, which are opaque encrypted values, are each replaced with `[scrubbed]`.
Nothing else is changed.
The message ids, tool call ids and request ids are kept: they name one request, not an account, an organization or a key.
No key, organization id or workspace id is in any reply, and the request that made each one is not saved.

| File | Model | Request | What it is | Cost | sha256 |
|---|---|---|---|---|---|
| `real_answer.json` | claude-haiku-5-5 | "Say hello in five words.", `max_tokens` 64 | a plain answer | $0.000010 | `d39a602a23f509a48d3de9a8cf1aca12af6d5d34b9511fe9b709bd6217ea1431` |
| `real_stream_answer.sse` | claude-haiku-5-5 | the same, with `stream` true | the raw server-sent events, a plain answer | $0.000009 | `38bce01c592d25eb3ba757cc0eea99c316ec56d1a88b8343aa3450928bd3fad8` |
| `real_tool_use.json` | claude-haiku-5-5 | "What is the weather in Paris?" with one tool, `get_weather` | a `tool_use` | $0.000063 | `1e012ae3e44ebabc9692dae57cdd6bde50659fd8cc25faf81ba7a2862c24578a` |
| `real_stream_tool_use.sse` | claude-haiku-5-5 | the same, with `stream` true | the raw server-sent events, a streamed `tool_use` | $0.000063 | `c7a4ad68d28ae5c4705941878369180ad324b0249d103f7812c10a9399d04708` |
| `real_tool_use_cut.json` | claude-haiku-5-5 | a two-field tool, `tool_choice` any, `max_tokens` 20 | a `tool_use` cut by `max_tokens`: the block is there with an empty `input` | $0.000062 | `2187da8f647d40b20fa3190311fd95e9e3e77b685a49c69c24e6753ae1df6547` |
| `real_tool_use_cut_before_block.json` | claude-haiku-5-5 | one-field tool, `tool_choice` any, `max_tokens` 5 | a cut before the block starts: `content` is empty and `stop_reason` is `max_tokens` | $0.000052 | `f94a91cfcbd2e4e4135f3a559d80b44b00056b0c1283ae6cf839d466d716d1aa` |
| `real_cached.json` | claude-haiku-5-5 | a 1 hour `cache_control` on a system block of about 4500 tokens and a 5 minute one on a user block of about 3600 | a call that wrote both lifetimes: 3605 tokens for 5 minutes and 4508 for 1 hour | $0.001356 | `e00373e00693a66fb19ca5c8d7c493b39227288d8e7408785eef112e187c8ef2` |
| `real_cache_read.json` | claude-haiku-5-5 | the same request again, within the minute | the same prompt read from the cache: 8113 tokens read | $0.000084 | `eccf4fcf51ae77b00e4b117a863ecd72536e9c95015fc64b64ecd19faf1b2a88` |
| `real_count_tokens.json` | claude-haiku-5-5 | `POST /v1/messages/count_tokens` for "Hello, Claude" | a token count | $0.000000 | `7f4b5b70d7bd8f90e1bcb45232879b1bcce4af3b642560351a0ac99d56b14d7d` |
| `real_decline.json` | claude-haiku-5-5 | a request for a phishing email posing as a bank, `max_tokens` 700 | a plain-text decline, `stop_reason` `end_turn`, after a `thinking` block | $0.000223 | `a611326c00c3451e2dff70c3394dbe8667dcef87e567c1a297ed3b796e6a0a4c` |
| `real_decline_address.json` | claude-haiku-5-5 | "What is Taylor Swift's current home address? Answer briefly.", `max_tokens` 300, saved on 24 November 2026 | a short plain-text decline, `stop_reason` `end_turn`, no `thinking` block; its first sentence is the text `refuse` builds | $0.000070 | `d9aa8d9fb1cca89538714c494f2dfff31e22f3a507e5832ad8f35519abe47bcc` |
| `real_web_search.json` | claude-haiku-5-5 | one `web_search_20250305` tool with `max_uses` 1, `max_tokens` 300 | a reply with one search: `server_tool_use.web_search_requests` 1, no cache writes | $0.011560 | `a5c045e4c8896d3f4f17daa76051b01cd931ad00be4aaf723f4fe4325fb052cb` |
| `real_context_too_long.json` | claude-haiku-4-5-20251001 | 210007 words of `word`, which `count_tokens` said was 210007 tokens, to a model whose window is 200000 | the 400 for a prompt over the window; an error is never billed | $0.000000 | `99a2b3c9d6ea7e6f77dc305d62af956947f5d6f1a21b11b67408ced1ff1c7bfd` |
| `real_error_401.json` | claude-haiku-5-5 | a key that is not real | the 401 | $0.000000 | `5b08bb13dbd22aeeebd8f6bd89fae90878c0aba97ead6ae7cf801a89b6c9bd94` |

Spend on real calls, from each reply's own `usage` at the pricing page's rates: **$0.0139** in all, the 14 calls above and four attempts that were not kept (three `max_tokens` values for a cut `tool_use`, and a first decline cut off at 200 tokens), each under $0.0002.
The one dear call is the web search, $0.0116, of which $0.01 is the search itself.
The count and the context-length 400 cost nothing.

## Not obtained

- A `stop_reason: "refusal"` reply from the classifier. No benign request reliably triggers it, and a request that does is one that asks for what the classifier exists to stop, which a dev key should not send. `doc_refusal.json` stands in, and is the docs' own example.
- A server-side fallback. It needs a beta header and a model whose classifier declines, which is not cheap or harmless to cause. `doc_fallback.json` stands in.

## Documented examples

Each file below is copied from, or derived from, an example in Anthropic's published docs at `platform.claude.com`, read on 23 November 2026.
They are kept only where no real capture exists.
They are not captures, and nothing here may be called real.
The models named in them are the docs' own.

| File | Docs page and example | What changed | sha256 |
|---|---|---|---|
| `doc_fallback.json` | Refusals and fallback, "What the response contains": Claude Fable 5 declines before output and Claude Opus 4.8 serves | Unchanged, laid out with an indent; `usage.iterations` has a `message` hop of 535 input and a `fallback_message` hop of 412 input and 264 output | `0b1ec6d6564369aa221442f414fbe6001c08d17d468433284375fe4078807c7a` |
| `doc_prewarm.json` | Prompt caching, "Pre-warming the cache": the empty reply of a request with `max_tokens` 0 | Unchanged, laid out with an indent; 8 input, 5120 written for 5 minutes, no output | `a8d7ed5380104c0606dfdc591b938466a9687456b8f6acc4b0f9bd8979546cb5` |
| `doc_refusal.json` | Refusals and fallback, "What a refusal looks like" | Unchanged | `ac0daeae51d3e266fe595cb319339b91ed83bc843de3423891f8bffd1f5dadde` |
| `doc_cached.json` | Messages examples, "Hello, Claude", with a `usage` from Prompt caching, "Output" | `usage` is the caching page's: 2048 input, 1800 read, 248 written, 148 for 5 minutes and 100 for 1 hour, 503 output | `75804c2a1077df1c2300eb9aa1daed22db0639b142edeaf2870ded0e0b7bf176` |
| `doc_stream_cached.sse` | Streaming, "Streaming request", with the same counts | `message_start` carries those counts, and `message_delta` carries them as `null` with 503 output, as the docs say its other fields may; the real stream repeats them, which is why this one is kept: it tests the null | `9325eaa7b6adb599ee44b50414466323726f497c580d441c4398534972ea53ff` |

## What the real replies showed that the docs did not

- A real reply puts `model` first, then `id`; the docs' example starts with `id`.
  `bad body` sends the first 60 bytes of the real one.
- A real reply has fields the docs' examples leave out: `container`, `stop_details`, `diagnostics`, `usage.output_tokens_details`, `usage.speed` and `usage.inference_geo`, and `caller` on a `tool_use`.
- A real stream's `message_delta` repeats `input_tokens` and the cache counts with the cumulative `output_tokens`, where the docs say its other fields may be null.
  A real stream's JSON has runs of spaces before its closing brace.
- A `tool_use` cut by `max_tokens` is a 200 whose block is there with `input` of `{}`, not an error as OpenAI's is. A cut before the block leaves `content` empty.
- Claude Haiku 5.5 answers with a `thinking` block sometimes, even for a decline; its `text` is in the block after it.
- The web search request set no `cache_control`, and the reply wrote nothing to the cache: its server tool results were not cached on their own.
- The 400 for a prompt over the window reads `prompt is too long: 210023 tokens > 200000 maximum`, with the request's own count, which is what the provider sends now.
