# Prices

How Scrimmage prices a model call, for OpenAI and for Anthropic.
The [budget](budget.md) uses these prices to hold and settle each call.

## Where prices come from

Scrimmage looks a model's price up in three places, in this order.
The first place that names the model gives the whole price.

1. `[agent.prices]` in `scrimmage.toml`.
2. `scrimmage-runs/prices.toml`, beside `scrimmage.toml`, which `scrimmage prices update` writes.
3. The `prices.toml` shipped with Scrimmage, which holds OpenAI's and Anthropic's prices.

A price file has one section for each provider, and a call is priced from the section of its own provider.
A row of the same name in another provider's section is not used.
`[agent.prices]` is flat, by model name, and speaks for a model whatever its provider.

A dated name, such as `gpt-4o-mini-2024-07-18` or `claude-haiku-4-5-20251001`, uses its family's price when the place it is looking in does not name it exactly.
OpenAI dates a snapshot `-YYYY-MM-DD` and Anthropic `-YYYYMMDD`.
Eight digits at the end of a name that are not a date, such as `-12345678`, are part of the name, so the name has no family.
A place higher in the list speaks first: your `[agent.prices]` row for `gpt-4.1` also prices `gpt-4.1-2025-04-14`, whatever a file holds for the dated name.
A model that none of them names is refused before the call, with the line to paste under `[agent.prices]`.
Scrimmage never guesses a price.

A price file has a section for each provider.
A section has the date of its prices, the page they came from, and a table for each model under `models`:

```text
[openai]
url = "https://developers.openai.com/api/docs/pricing"
as_of = "2026-11-22"

[openai.models."gpt-4.1"]
input = 2.00
output = 8.00
cached_input = 0.50

[openai.models."gpt-4.1".tiers.flex]
input = 1.00
output = 4.00
```

`url` and `as_of` are required in each section, and a model takes the keys of `[agent.prices]`.
A section may also have a `[provider.fees]` table, such as `[anthropic.fees]` with `web_search_request = 0.01`: dollars for each request of that kind, which every model of the provider pays unless its own row says otherwise.
A fee is read like a rate, and may not be negative.
A section's name is the provider's, such as `openai`.
A section for a provider this version of Scrimmage does not gate is read and kept, and never used.
`scrimmage check` warns of such a section in `scrimmage-runs/prices.toml`, which may be a typo in its name.
A file in the old format, with `as_of`, `source` or `models` at the top, is refused with the line to run `scrimmage prices update`.
A file may not make a model free, because that would turn the budget off for it.
Every `input`, in a model or a tier, must be at least 0.001 dollars per million tokens, and so must every `output`, unless the model's name contains `embedding`.
A `cached_input` or cache-write rate, if you give one, must be at least 0.001 too, even for an embedding model.
That is far below any real price, the cheapest today being 0.01.
A tier cannot be named `default` or `auto`, in any case.
Only `[agent.prices]` may price a model at 0, as your own choice.
A file that fails its checks is exit 2, and the message names the file and the line.
`scrimmage check` prints the date of each provider's prices on its budget line, such as `OpenAI prices as of 2026-11-22, Anthropic prices as of 2026-11-25`, and, when you have prices of your own, the source of each.
A price is only as new as its file, so run [`scrimmage prices update`](cli.md#scrimmage-prices-update) now and then.

A call whose reply states its own cost, such as an OpenRouter reply's `usage.cost` or the LiteLLM proxy's `x-litellm-response-cost` header, is settled at the larger of that cost and the cost of its tokens at your prices.
Scrimmage adds nothing to the request to ask for it, and a model with no price is still refused before the call, because the hold needs one.

## The shipped OpenAI prices

Scrimmage ships prices for OpenAI models, cached input included, checked against OpenAI's pricing page on 22 November 2026.
They cover `gpt-4o`, `gpt-4o-mini`, the `gpt-4.1` family, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-pro`, `gpt-5.1`, `gpt-5.2`, `gpt-5.2-pro`, `gpt-5.3-codex`, `gpt-5.4`, `gpt-5.4-pro`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.5`, `gpt-5.5-pro`, `gpt-5.6-terra`, `gpt-5.6-luna`, the `gpt-6` family, `o3`, `o3-pro`, `chat-latest`, `gpt-5-search-api` and the `text-embedding` models.
`gpt-4o-2024-05-13` and `gpt-4.1-nano` are no longer on that page, so their prices are the ones listed on 7 November 2026.
A model with a higher price above 272K input tokens has a `long` table with the price of a call over that size, charged on the whole call.
Those are `gpt-5.4`, `gpt-5.4-pro`, `gpt-5.5`, `gpt-5.5-pro`, `gpt-5.6-terra`, `gpt-5.6-luna` and every `gpt-6` model.
The page lists these service tiers, and the file holds each for these models only:

| Tier | Models |
|---|---|
| `flex` | `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1`, `gpt-5.2`, `gpt-5.4`, `gpt-5.4-pro`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.5`, `gpt-5.6-terra`, `gpt-5.6-luna`, `o3`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-6.1-sol` and `gpt-6.1-terra` |
| `fast`, also found as `priority` | `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-5`, `gpt-5-mini`, `gpt-5.1`, `gpt-5.2`, `gpt-5.3-codex`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.5`, `gpt-5.6-terra`, `gpt-5.6-luna`, `o3` and every `gpt-6` model |
| `ultrafast` | `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.2-astra` and `gpt-6.2-sol` |

`fast` and `priority` are one tier under two names, so a price that holds one of them prices both.
`gpt-6.2-astra` and `gpt-6.2-sol` list a promotional Ultrafast rate, 3 times Standard, until 28 February 2027, and 6 times Standard after.
The file holds the 6 times rate, so those two tiers cost twice the bill until that date.
A reply that names a tier a model's row does not list is settled at the dearest rates the row does list, and its record line has `unpriced_tier` set and `cost_estimated` true.
See [Long-context prices](#long-context-prices) and [Service tiers](#service-tiers).
`gpt-5.6-sol` is not in the table: the page lists only a promotional price, which ends on 30 November 2026.
Give it under `[agent.prices]`.
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

The hold before a call still counts every input token at the full price, since Scrimmage cannot know what the provider will serve from its cache.

A call that sends no model name, such as a Responses call on a stored prompt, is priced by the key `""`:

```toml
[agent.prices]
"" = {input = 2.0, output = 8.0}
```

With a budget on, a model with no price stops the test at its first call, because it cannot be held for.
With `max_cost_usd = "none"`, no price is needed, and tokens are still counted.

A reply without usable token counts gets a rough cost, worked out from how many bytes went each way, and its record line has `cost_estimated` set.
On Chat Completions a stream only carries counts if your request sets `stream_options.include_usage`.
Scrimmage leaves your request as it is, so without that setting those costs are rough ones.
The Responses API always sends counts in its last event.

### Cache writes

On newer OpenAI models, such as `gpt-5.6-luna`, a token written to the prompt cache costs more than a plain input token: 1.25 times the input price for 30 minutes, 2.5 times for 12 hours.
OpenAI bills each input token once, as plain input, as cached or as written, so the cached and written counts are parts of the input count.
Scrimmage reads the written count from `input_tokens_details.cache_write_tokens` on Responses, split by `cache_write_tokens_details.ttl_30m_tokens` and `ttl_12h_tokens`, and from `prompt_tokens_details.cache_write_tokens` on Chat Completions, where writes last 30 minutes.
OpenAI states that the counts are parts of the input count for Responses only.
On Chat Completions, Scrimmage reads the field the openai library names in the same way, and no real Chat Completions reply with writes has confirmed it yet.

| Reply | Price of each input token |
|---|---|
| 12000 input tokens: 4000 cached, 3000 written for 30 minutes, 4000 for 12 hours, on `"gpt-5.6-luna" = {input = 0.2, cached_input = 0.02, cache_write_30m = 0.25, cache_write_12h = 0.5, output = 1.2}` | 1000 at $0.20, 4000 at $0.02, 3000 at $0.25 and 4000 at $0.50 per million |
| a price with no `cache_write_30m` or `cache_write_12h` | written tokens at `input`, as on a model that charges nothing extra to write |
| a written count above the input tokens not cached, or not a whole number | every input token not cached as written for 12 hours |
| a split by lifetime with no written count | every input token not cached as written for 12 hours |
| no usable counts, a call that failed after it was sent, or a redirect off the model host | the rough cost prices every input token at the highest of `input`, `cache_write_30m` and, on Responses, `cache_write_12h` |

The written counts are not on the record line; `cost_usd` includes them.
The hold before a call counts every input token at the highest of `input`, `cache_write_30m` and `cache_write_12h`, since the provider may write any of them to its cache.
On Chat Completions, where writes last 30 minutes, it leaves out `cache_write_12h`.
On `gpt-5.6-luna`, 100K input tokens and 4096 output tokens hold $0.0549 on Responses and $0.0299 on Chat Completions.

## Anthropic prices

The shipped file prices the current Claude models, from Anthropic's pricing page and fast mode page, with each model's cache reads, its 5 minute and 1 hour cache writes, and a `tiers.fast` row for the models that have fast mode.
Claude Haiku 5.5 has a `long` table: Anthropic charges the whole request at the higher rates when its prompt is over 100,000 tokens.
A web search is settled at $0.01, the page's $10 for 1,000 searches, from `[anthropic.fees]`.
It leaves out the Batch API, US-only inference at 1.1 times, data residency multipliers and the retired models; the date of the prices is on the `scrimmage check` budget line.

To override a shipped Anthropic price, name the model under `[agent.prices]`.
Your row replaces the shipped one whole, so give the cache rates, the fast tier and the `long` table you use as well.

```toml
[agent.prices."claude-haiku-5-5"]
input = 0.10
cached_input = 0.01
cache_write_5m = 0.125
cache_write_1h = 0.20
output = 0.50

[agent.prices."claude-haiku-5-5".long]
above = 100000
input = 0.50
cached_input = 0.05
cache_write_5m = 0.625
cache_write_1h = 1.0
output = 2.50
```

A price here replaces a file's price whole: `gpt-4o = {input = 2.5, output = 10}` has no cached price, even though the shipped file has one.

## Service tiers

OpenAI bills a call at the tier it was served on, such as `default`, `flex` or `priority`.
The reply names the tier, and Scrimmage settles the call at that tier's rates.
Before the call it holds the budget at one tier's rates.
That is the tier the request names, else `[agent] service_tier`, else the default tier.
A request that names `auto` names no tier.

```toml
[agent]
service_tier = "priority"

[agent.prices."gpt-4.1"]
input = 2.0
output = 8.0

[agent.prices."gpt-4.1".tiers.priority]
input = 3.5
output = 14.0
```

Scrimmage never adds a tier to the request: it would change what the model call does.

`fast` and `priority` are one tier under two names, since OpenAI renamed priority processing to Fast mode.
A price that holds only `tiers.fast` or only `tiers.priority` prices both names, in `[agent.prices]` as in a price file.
If a reply names a tier that the price does not list, such as `scale`, the call is settled at the dearest rates the price does list, long rates included.
Its record line then has `unpriced_tier` set and `cost_estimated` true.
The hold stays at the tier the request names, else `[agent] service_tier`, else the default tier.

A price here replaces the shipped row whole, its tiers and its `long` table too.
If you price a model the file ships, such as `gpt-5.6-luna`, give its `long` table and the tiers you use as well.
See [Long-context prices](#long-context-prices).

## Long-context prices

Some models cost more per token once the prompt is long.
OpenAI charges `gpt-5.5` and `gpt-5.4` twice the input price and one and a half times the output price when a call has more than 272K input tokens.
The whole call is charged at the higher rates, not only the tokens past the threshold.

```toml
[agent.prices."gpt-5.5"]
input = 5.0
cached_input = 0.5
output = 30.0

[agent.prices."gpt-5.5".long]
above = 272000
input = 10.0
cached_input = 1.0
output = 45.0
```

`above` is a whole number of input tokens, at least 1.
A call whose input tokens are more than `above` is settled at the `long` rates, and a call held before it is sent is held at them when its estimated input is more than `above`.
A call of exactly `above` tokens costs the normal rates.
Each `long` rate must be at least the same rate of the model, so a long call never costs less per token.
A rate that `long` leaves out costs its `input`, as it does for the model.
A tier may have its own long rates, such as `[openai.models."gpt-5.5".tiers.flex.long]`; a tier with none costs its own rates at any length.
`long` works the same in a price file and in `[agent.prices]`.
