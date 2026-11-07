# Configuration: derail.toml

One `derail.toml` describes one agent and its tests.
Derail names the table and key of every bad value, such as `[agent] run_timeout: expected a number of seconds, got 'abc'`.
Anything this version does not support is rejected as unknown, with exit code 2. Nothing is ever silently ignored.

## [agent]

| Setting | What it does | Default |
|---|---|---|
| `function` | The agent, as `module:function`. Derail calls it with the question and waits for it if it is async. Give this or `command`. | none |
| `command` | The agent as a script, such as `"python bot.py {question}"`. It is split into arguments like a shell's words, and the question fills `{question}` as part of one argument, with no shell. Must hold `{question}`. Give this or `function`. See [An agent run as a script](scripts.md). | none |
| `children` | `true` fires faults in child processes too, and lets a child on another Python join if it has Derail installed. See [Faults in children](scripts.md#faults-in-children). | `false` |
| `tools` | The agent's own tools, as `module:function` import paths, or `module:Class.method` for a tool that is a method. Derail swaps each one for a gate when your code imports its module. When the model sees a different name, write `{path = "package.tools:lookup_order", name = "lookup-order"}`; labels use the name the model sees, which for a method is its own name. See [Tools that are methods](#tools-that-are-methods). | empty |
| `real` | Calls that are safe to run for real when a test does not mock them, such as `"tool:check_inventory"`, `"tool:*"` for every tool, `"mcp:docs/search"` for a tool on an MCP server, `"hosted:mcp/orders"` for a hosted MCP server, `"process:python"` for a Python child that does not join, or a web write such as `"http:orders.acme.internal POST /refunds"`. Model calls are not listed: they are real unless a fault names them. | empty |
| `runs` | How many times each row runs. | 3 |
| `parallel` | How many runs go at the same time. | 4 |
| `run_timeout` | Seconds one run may take before it is stopped and marked "timed out". | 120 |
| `max_cost_usd` | The most the whole test may spend on model calls, in dollars. `"none"` turns the budget off, on purpose. | 5 |
| `model_hosts` | Extra hosts whose requests are model calls, such as a gateway, each with the format it speaks: `[{host = "llm-gateway.internal", provider = "openai"}]`. A host name only, with no port. | empty |
| `reserve_output_tokens` | The output tokens a budget reservation assumes when a request does not set `max_tokens`. A call that does not fit while others hold the budget waits for them; see [When calls in flight hold the budget](model-faults.md#when-calls-in-flight-hold-the-budget). | 4096 |
| `prices` | Prices for other models, as the table below. The key `""` prices a call that sends no model name. | empty |
| `answer` | Where the answer is in what the function returns, such as `"messages[-1].content"`. Only with `function`. Required when the function returns anything but text, as the whole value would hold tool results too. See [Agents on a framework](frameworks.md). | the returned text |

## [agent.prices]

Prices for models the built-in table does not know, in dollars per million tokens.

```toml
[agent.prices]
llama-70b = {input = 0.6, output = 0.6}
acme-gpt = {input = 2.0, cached_input = 0.5, output = 8.0}
```

| Key | What it is |
|---|---|
| `input` | The price of an input token. Required. |
| `output` | The price of an output token. Required. |
| `cached_input` | The price of an input token the provider served from its prompt cache. Optional, and at most `input`. Without it, cached tokens cost the full `input` price, which can only overstate the cost. |

A price here replaces the built-in one whole: `gpt-4o = {input = 2.5, output = 10}` has no cached price, even though the built-in table has one.

Derail imports only modules under your agent's own top-level packages, the ones `function` and `tools` name. For a script, that is the ones `tools` names.

## Tools that are methods

Many agents define their tools as methods on a class, and call them on an instance:

```python
# shop/tools.py
class StoreTools:
    def __init__(self, database: str) -> None:
        self.database = database

    def get_order(self, order_id: str) -> str: ...

    @staticmethod
    def check_inventory(sku: str) -> str: ...
```

List each method by its class:

```toml
tools = ["shop.tools:StoreTools.get_order", "shop.tools:StoreTools.check_inventory"]
```

The model sees `get_order`, so the label is `tool:get_order`.
Derail gates the method on the class when the module loads, so every instance goes through the gate, whether it was made before or after.
Mocks and faults work as on a function. A mock is built from the method's return annotation.
The record never holds `self` or `cls`, only the arguments after them.

| What the path names | What happens |
|---|---|
| a method, a `staticmethod` or a `classmethod`, sync or async | gated, of the same kind |
| a method a base class defines, named through a subclass, `StoreTools.get_order` | gated on `StoreTools` and its subclasses; the base class and its other subclasses are left alone |
| an alias in the class body, `fetch = get_order` | gated with the method, under the same label |
| a method of a class whose metaclass refuses new attributes | the run stops, with the reason |
| the class itself, `shop.tools:StoreTools` | the run stops: list each method instead |
| a property or a plain attribute | the run stops: it is not a method |
| an attribute your code sets in `__init__` | the run stops: Derail gates the class when the module loads, before any instance exists, so define it as a method |
| a method on a nested class, `Outer.Inner.method` | refused when the file is read: move the class to the top of its module |

## [[test]]

| Setting | What it does |
|---|---|
| `name` | A name for the test, unique in the file. Required. |
| `question` | The question the agent gets. Required. |
| `expect` | What the answer must hold: a list, every item must match, and an item is a text or a list of texts of which any one is enough. `["shipped", ["Oct 30", "October 30"]]`. Required. See [Judging an answer](judging.md). |
| `forbid` | A list of texts the answer must not hold. Optional. |
| `pass_rate` | The share of judged runs that must pass, above 0 and up to 1, such as `0.8`. Every judged run by default. More than half the planned runs must be judged for any result. |

## [[test.mock]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to mock, such as `"tool:get_order"`, `"mcp:docs/search"` or `"http:inventory.acme.internal"`. A model call cannot be mocked: it is real or a fault. Required. |
| `returns` | The value to return, in TOML: text, a number, true or false, a list or a table. Required for a tool and an MCP tool. Dates and times are not allowed; write them as text. |
| `type` | On a Python tool: the type to build the value into, as `module:Class`. Optional; read from the tool's return annotation when left out. |
| `method` | On a web request: match only this method. |
| `path` | On a web request: match only this exact path, with no query string. |
| `status` | On a web request: the status of the reply. 200 by default. |
| `headers` | On a web request: extra reply headers, as a table of text. |
| `json` | On a web request: the body, as JSON. |
| `text` | On a web request: the body, as text. |

See [Web requests](web-requests.md) and [MCP tools](mcp.md).

## [[test.fault]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to fail, such as `"tool:get_order"`, `"mcp:docs/search"`, `"model"` or `"http:inventory.acme.internal"`. Required. |
| `do` | The fault: `"timeout"`; on a tool or an MCP tool `"error"`; on an MCP tool `"empty"`; on a model call or a web request `"status 429"` or any status from 400 to 599. Required. |
| `raises` | For `"error"` on a Python tool: the error to raise, a built-in such as `"ConnectionError"`, or `package.module:Class` from your agent's packages. Optional. |
| `when` | When it fires: `"always"`, the default; `"first N"`, the first N attempts; or on a model call `"requests N"`, the first N requests and every retry of each. |
| `method` | On a web request: fail only this method. |
| `path` | On a web request: fail only this exact path, with no query string. |
| `model` | On a model call: fail only calls to this model, such as `"gpt-4.1"`. The whole name your code sends, compared exactly. See [Failing one model](model-faults.md#failing-one-model). |
| `expect` | Replaces the test's for this row. A fault that fires on every call needs this or `forbid`; with `first N` it can use the test's. See [Judging an answer](judging.md). |
| `forbid` | Replaces the test's for this row. `[]` drops the test's list. |
| `pass_rate` | Replaces the test's for this row. |

## Coming in later versions

These are part of Derail's design but not of this version.
Using one now stops the test: it is rejected as unknown.

- Agent shapes: `service` (a web service).
- Settings: `body`, `headers`, `control`, `control_tls`, `records`, `record_grace_s`, `record_wait_s`.
- Labels: `auth:`, and model providers other than OpenAI.
- Faults: `slow`, `empty` on a tool or a web request, `refused`, `reset`, `dns`, `bad body`, `auth error`, `context too long`, `append`, `replace`, the model reply faults, and `status` on a tool.
- Schedules: `once` (write `first 1`) and `call N`.
- Fault options: `retry_after`, `append`, `replace`.
- Mocks: `returns_from`.
- Placeholders: `${VAR}`.
