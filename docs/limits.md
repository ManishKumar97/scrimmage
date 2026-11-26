# Limits of this version

## Will Scrimmage work for your agent?

Six limits decide this.
Check them first.
Every other limit is on one of the pages listed below, by area.

- **OpenAI and Anthropic only.** Model calls are understood for OpenAI's API, for Anthropic's Messages API, and for gateways that speak either format; a call to another provider stops the test until you list it.
  Either name a gateway that speaks OpenAI's or Anthropic's format under `model_hosts`, or list the provider's host under `real`, where its calls run with no budget and no faults; see [Use a gateway](model-faults.md#use-a-gateway).
  Anthropic's Messages API is recognized and its calls are counted, and the shipped price file prices the current Claude models, so a call with a budget on runs; a model it does not list is refused until you add a price under `[agent.prices]`.
  `replace`, `garble`, `truncate`, `refuse` and `unknown tool` work on an Anthropic reply, and `bad tool args` never will be: see [Reply faults on an Anthropic call](model-faults.md#reply-faults-on-an-anthropic-call) and [`bad tool args` on an Anthropic call](model-faults.md#bad-tool-args-on-an-anthropic-call).
- **LangChain only.** LangChain tools, which LangGraph runs, are found by themselves; a tool on another framework is not gated.
  List each tool's function under `tools`; see [the `tools` setting](configuration.md#agent).
- **httpx only.** Web requests through httpx and httpx2 are gated; `requests`, `aiohttp`, `urllib` and raw sockets are not gated: nothing mocks, faults or refuses their requests, and they leave only an `ungated` notice line in the record.
  A write through `requests` or `aiohttp` reaches the real system.
  A run that sends a request through `requests`, `urllib3`, `urllib` or `aiohttp` says so in the report, with the library, the host and the method.
  The openai SDK uses httpx2, so model calls are covered.
  Move the call to httpx or httpx2, or list the tool that makes it under `tools`; see [What is covered](web-requests.md#what-is-covered).
- **No streaming replies.** A service that streams its answer stops the test with exit 2, and a streamed model or web reply cannot take `replace` or `append`.
  Test a route that returns one reply; see [Each run](services.md#each-run).
- **Rules match text, not meaning.** `expect` finds text, so "shipped" passes inside "has not shipped".
  Write a `forbid` for the wrong answer too; see [How text is compared](judging.md#how-text-is-compared).
- **Install from git.** Scrimmage is not on PyPI yet; the name there holds an empty placeholder.
  Install it from a clone; see [From a clone](install.md#from-a-clone).

If none of these applies, the area pages below are detail for your use case.

## Limits by area

- [How your agent runs](limits/agents.md): writing and checking a test, a function or a script, a web service, Docker Compose, Kubernetes, and programs and processes.
- [Tools, MCP and web requests](limits/calls.md): tools and frameworks, MCP tools, web requests and recorded mocks.
- [Faults and model calls](limits/model.md): the faults on a model call, and the providers and replies Scrimmage reads.
- [The budget and prices](limits/budget.md): how a call is held and settled, and the shipped prices.
- [Judging and records](limits/judging.md): judging an answer, and what a record keeps and removes.
