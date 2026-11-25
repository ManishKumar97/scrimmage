# Limits: the budget and prices

Limits of how a call is held and settled, and of the shipped prices.

## The budget


- **A call can cost more than its hold.** The hold counts the input as 4 bytes a token and the output as the call's own limit, times `n` on Chat Completions, which bills the output of every choice.
  It falls back to `reserve_output_tokens` when the call sets no limit, when the limit is not a positive whole number, and when Scrimmage cannot read the request body: a compressed body, one sent from a generator, or a multipart one.
  A reply that writes more, or an input with more tokens than that, such as an image, costs more than was held, and its full cost is counted.
  So calls in flight at once can pass `max_cost_usd`, by up to what they cost above their holds, added together.
  Three `gpt-4.1` calls with no `max_tokens` each hold about $0.0328, so all three fit in $0.10 at once; if each writes 8192 output tokens, the test spends $0.1967.
  Once money spent passes the limit, every later call is refused at once.
  Set `max_tokens`, `max_completion_tokens` or `max_output_tokens` on each call, in a body Scrimmage can read, to bound its output, `n` choices included; nothing bounds the input estimate.

- **Regional and FedRAMP endpoints cost more.** The shipped prices are OpenAI's global rates. Regional (data residency) endpoints and FedRAMP endpoints charge a 10% uplift on models released on or after 5 March 2026. A team on those endpoints sets its own rates under `[agent.prices]`, or its calls settle 10% low.

- **A call served on a dearer tier than its hold.** A call is held at the service tier its request names, else at `[agent] service_tier`, else at the default tier, and is settled at the tier the reply names. OpenAI can serve a call on a dearer tier than that, such as `priority` for a request that names none. It then costs more than its hold, and calls in flight can pass `max_cost_usd` by the difference, as for the hold above. Set `service_tier` to your account's tier to avoid it. See [Service tiers](../prices.md#service-tiers).

- **A gateway's stated cost can only raise a call's cost.** A reply that states its cost, such as OpenRouter's `usage.cost` or the LiteLLM proxy's `x-litellm-response-cost` header, is settled at the larger of that figure and the cost of its tokens at your prices. A gateway that reports 0, or only its own fee, as OpenRouter does for a key you brought, or that does not know a model, as LiteLLM does for one missing from its cost map, would otherwise turn `max_cost_usd` off. So a gateway that charges less than your price file says is still counted at your price file's figure.

- **`scrimmage prices update` trusts this repository's `main`.** It writes the file it finds there, after checking its schema and refusing a rate below $0.001 per million tokens, except the output rate of a model whose name contains `embedding`, and it prints every change before it writes. It does not compare the new rates with the old ones, so a price set too low there, but above that floor, would let a test spend more than its budget intends. Read the printed changes before you run a test on them.

- **Prices need checking.** The shipped price file is small and as old as your installed version; the `as_of` date of each provider's section shows on `scrimmage check`. Run `scrimmage prices update` for the newest, check it against your provider's pricing, and use `[agent.prices]` for anything else.

- **Two cache-write rates only Anthropic uses.** `cache_write_5m` and `cache_write_1h` are valid in a price file and in `[agent.prices]`, with the same checks as the other write rates. A call to OpenAI never settles at them, because OpenAI writes its cache for 30 minutes or 12 hours. A call to Anthropic settles at them, and a row that leaves one out is settled at Anthropic's own multiple of its `input`: 1.25 times for 5 minutes and 2 times for 1 hour, as its prompt caching page says.

- **Anthropic's `inference_geo` of `us` is priced at the normal rate.** A request that sets it costs about 10% more at Anthropic, 1.1 times, and Scrimmage settles it at the row's normal rates, so its cost is about 10% low. Set your own price for the model under `[agent.prices]` with the 1.1 times in it.

- **An Anthropic call that more than one model serves.** With server-side fallback, `usage.iterations` lists each attempt, and each is settled at the row of the model that ran it, the attempt that declined included, which Anthropic bills only in some refusal categories, so the cost can be high by that attempt. A request with `fallbacks` is held for every attempt: each model named at its own row, at the larger of its entry's `max_tokens` and the request's, and at its entry's `speed` when it sets one. Each attempt is also held for the request's web searches.
A model with no row is held and settled at the dearest row there is. Every attempt of `"default"` is held at the dearest row, because its model is not known until the reply, and settled at the row of the model that ran it.
The reply does not say which speed each attempt ran at, only the models and the tokens, so Scrimmage reads it from the request, as the hold does: the first attempt at the request's `speed`, and a fallback at its entry's `speed`, or the request's when the entry sets none.
When two entries name the same model, or none names the model that ran, the fallback is settled at the dearer speed. When fast mode is not available and Anthropic runs a fast attempt at standard, that attempt is settled high.
An attempt of another kind, such as an advisor, is settled at the reply's tier for the requested model and at the normal rates for any other.
A request Scrimmage cannot read, such as a compressed one, is settled the same way.
The hold covers only the models the request names, so a reply in which a model no entry names served can settle above its hold.

- **Anthropic's server tools.** A web search is settled at $0.01 each, from the searches the reply counts, unless `[anthropic.fees]` in a price file or `web_search_request` in a model's `[agent.prices]` row says otherwise. The hold adds `max_uses` searches, or 10 when there is none. A server tool loop adds input at Anthropic's side that the hold cannot see, and writes the cache by itself, so a hold can be low for such a call; the hold uses at least the 5 minute write rate. Code execution is billed by container time, which a reply never reports, so a budget cannot count it: a request that offers it is refused, with the reason `unmetered_tool`, unless it also offers `web_search_20260209` or `web_fetch_20260209` or later, with which it is free. The refusal does not depend on whether a budget is set.
  The searches are counted from the reply's own `usage.server_tool_use`, which no real reply Scrimmage has seen spreads over `usage.iterations`; a search an attempt of a fallback or an advisor ran, if the API counts it in that attempt's entry only, is not counted.

- **A long prompt costed from an estimate.** A model that charges more above a number of input tokens has a `long` price with an `above` threshold. A call is settled at the long rates when the reply's input tokens are over `above`. Before the call, its input is estimated from the size of the request, so a call whose estimate is under `above` but whose real input is over it is held at the normal rates, and calls in flight can pass `max_cost_usd` by the difference. A tier with no `long` price of its own costs its normal rates at any length. See [Long-context prices](../prices.md#long-context-prices).

- **A hold that cannot end while a call waits.** A call that its own run's calls would keep out, even if every other run finished, waits at most 30 seconds, or half the time its run has left. A stream the agent keeps open across a second call, or one it stopped reading without closing, keeps its hold, so the second call ends in a "not reserved" stop. A hold in another run is waited for until near this run's `run_timeout`.

- **A killed runner takes its runs with it, but not their children.** On Linux each run is asked to be killed with SIGKILL when the runner dies, even if it ignores SIGTERM. A run's own child processes are not covered, and on other systems nothing is set, so a run can outlive a runner killed there.

- **A killed run's holds stay.** A run killed with SIGKILL, such as one that ignores SIGTERM, can leave a hold that never settles, as Scrimmage cannot tell whether its call was sent. It counts as spent for the rest of the test, so a call it keeps out is refused at once, and the report warns about it.

- **The first in line waits for room.** A waiting call is served in turn: it goes ahead only when it fits along with every call ahead of it, so a large call at the head keeps a smaller one behind it waiting, even when the smaller one would fit alone. A call far back in a long line asks again only every 1 to 2 seconds, so when room frees for many calls at once it can go ahead up to 2 seconds late; with a short `run_timeout` that is a real share of the run.

- **A waiter keeps its place until it stops waiting.** A waiting call asks again as often as the runner says, from a quarter of a second at the head of the line to 2 seconds far back, and its place in line does not depend on how fast it asks, so a slow or loaded machine does not reorder the line. Its place ends when it is granted, when it gives up or is interrupted, or when its run ends, even by SIGKILL. A waiter that stops asking while its run goes on keeps its place for 60 seconds, then loses it, so a lost hand-back cannot block the line for good. Three cases can leave such a dead place: a child process killed while it waits, a process paused in a debugger, and an event loop blocked by sync code for 60 seconds. Until it goes, the calls behind it wait, and some are refused rather than delayed: a call kept out by its own run's holds waits at most 30 seconds, and a call kept out by another run's waits until a tenth of its own `run_timeout`, at most 10 seconds, is left, so with a `run_timeout` under about 70 seconds it can give up before the dead place goes.

- **Cached input that a gateway reports in its own fields.**
  Cached tokens are read from OpenAI's usage fields and, on an Anthropic call, from its cache read and cache write fields.
  A gateway that reports them another way, or a price under `[agent.prices]` with no `cached_input`, prices every input token at the full rate.
  The cost is then high, never low.

- **Cache writes on Chat Completions are unconfirmed.** OpenAI states for Responses that written tokens are part of the input count and apart from cached ones. Scrimmage reads Chat Completions' `prompt_tokens_details.cache_write_tokens` the same way, as the openai library names it, but no real Chat Completions reply with writes has confirmed it.
