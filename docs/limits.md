# Limits of this version

- **One agent shape.** The agent must be a Python function. Scripts and web services come later.
- **Tools and OpenAI model calls.** Faultline gates the tools you list under `tools`, and model calls the openai library sends. Other web requests, writes included, and MCP tools pass untouched and are not recorded yet. See [Safety](safety.md).
- **One provider.** OpenAI's Chat Completions, Responses and embeddings, and gateways that speak the same format. Other providers come later.
- **One HTTP library.** The gate sits in httpx2, which current openai versions use. Older openai versions, which use httpx, are not gated yet; with a budget on, the test then fails because no model call was seen.
- **asyncio only for async agents.** An async model call outside asyncio, such as under trio, is refused and ends its run, because Faultline cannot budget it. Synchronous agents are not affected.
- **One framework.** LangChain tools, which LangGraph runs, have an adapter. A tool on another framework is not gated, so offering it to the model stops the test.
- **A few faults.** `timeout` and `error` on tools, and `timeout` and `status N` on model calls. Faults that change the model's reply come later.
- **Prices need checking.** The built-in price table is small; check it against your provider's pricing, and use `[agent.prices]` for anything else.
- **Tools reached by import path.** A tool your code stores in a list or dictionary the moment it is defined keeps the original function, so the gate cannot reach it. A marker for those comes later.
- **No judging.** The answer is recorded, not compared with `expect`.
- **Child programs** on the same Python get the mocks and safety rules but no faults; a forked child's record lines are not sent yet.
- **Python only.** The SDK must run inside the agent's own Python program.
