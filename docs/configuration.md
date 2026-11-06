# Configuration: faultline.toml

One `faultline.toml` describes one agent and its tests.
Faultline names the table and key of every bad value, such as `[agent] run_timeout: expected a number of seconds, got 'abc'`.
Anything this version does not support is rejected as unknown, with exit code 2. Nothing is ever silently ignored.

## [agent]

| Setting | What it does | Default |
|---|---|---|
| `function` | The agent, as `module:function`. Faultline calls it with the question and waits for it if it is async. Required. | none |
| `tools` | The agent's own tools, as `module:function` import paths. Faultline swaps each one for a gate when your code imports its module. When the model sees a different name, write `{path = "package.tools:lookup_order", name = "lookup-order"}`; labels use the name the model sees. | empty |
| `real` | Calls that are safe to run for real when a test does not mock them, such as `"tool:check_inventory"`, `"tool:*"` for every tool, or `"hosted:mcp/orders"` for a hosted MCP server. Model calls are not listed: they are real unless a fault names them. | empty |
| `runs` | How many times each row runs. | 3 |
| `parallel` | How many runs go at the same time. | 4 |
| `run_timeout` | Seconds one run may take before it is stopped and marked "timed out". | 120 |
| `max_cost_usd` | The most the whole test may spend on model calls, in dollars. `"none"` turns the budget off, on purpose. | 5 |
| `model_hosts` | Extra hosts whose requests are model calls, such as a gateway, each with the format it speaks: `[{host = "llm-gateway.internal", provider = "openai"}]`. A host name only, with no port. | empty |
| `reserve_output_tokens` | The output tokens a budget reservation assumes when a request does not set `max_tokens`. | 4096 |
| `prices` | Prices for other models, as the table below. | empty |
| `answer` | Where the answer is in what the function returns, such as `"messages[-1].content"`. See [Agents on a framework](frameworks.md). | the whole value |

## [agent.prices]

Prices for models the built-in table does not know, in dollars per million tokens.

```toml
[agent.prices]
llama-70b = {input = 0.6, output = 0.6}
```

Faultline imports only modules under your agent's own top-level packages, the ones `function` and `tools` name.

## [[test]]

| Setting | What it does |
|---|---|
| `name` | A name for the test, unique in the file. Required. |
| `question` | The question the agent gets. Required. |
| `expect` | A list of text the answer should contain. Required; used for judging in a later version. |

## [[test.mock]]

| Setting | What it does |
|---|---|
| `call` | The label of the tool call to mock, such as `"tool:get_order"`. A model call cannot be mocked: it is real or a fault. Required. |
| `returns` | The value to return, in TOML: text, a number, true or false, a list or a table. Required. Dates and times are not allowed; write them as text. |
| `type` | The type to build the value into, as `module:Class`. Optional; read from the tool's return annotation when left out. |

## [[test.fault]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to fail, such as `"tool:get_order"` or `"model"`. Required. |
| `do` | The fault: `"timeout"`; on a tool `"error"`; on a model call `"status 429"` or any status from 400 to 599. Required. |
| `raises` | For `"error"`: the error to raise, a built-in such as `"ConnectionError"`, or `package.module:Class` from your agent's packages. Optional. |
| `when` | When it fires: `"always"`, the default; `"first N"`, the first N attempts; or on a model call `"requests N"`, the first N requests and every retry of each. |

## Coming in later versions

These are part of Faultline's design but not of this version.
Using one now stops the test: it is rejected as unknown.

- Agent shapes: `command` (a script) and `service` (a web service).
- Settings: `body`, `headers`, `control`, `control_tls`, `records`, `record_grace_s`, `record_wait_s`.
- Labels: `mcp:`, `http:`, `auth:`, and model providers other than OpenAI.
- Faults: `slow`, `empty`, `refused`, `reset`, `dns`, `bad body`, `auth error`, `context too long`, `append`, `replace`, the model reply faults, and `status` on anything but a model call.
- Schedules: `once` (write `first 1`) and `call N`.
- Fault options: `retry_after`, `append`, `replace`, and `method` and `path` to narrow a web-request fault.
- Mocks: `returns_from`, and web-request mocks (`method`, `path`, `status`, `json`, `text`).
- Placeholders: `${VAR}`.
