# Configuration: scrimmage.toml

One `scrimmage.toml` describes one agent and its tests.
Scrimmage names the table and key of every bad value, such as `[agent] run_timeout: expected a number of seconds, got 'abc'`.
Anything this version does not support is rejected as unknown, with exit code 2. Nothing is ever silently ignored.

## [agent]

| Setting | What it does | Default |
|---|---|---|
| `function` | The agent, as `module:function`. Scrimmage calls it with the question and waits for it if it is async. Give exactly one of `function`, `command` and `service`. | none |
| `command` | The agent as a script, such as `"python bot.py {question}"`. It is split into arguments like a shell's words, and the question fills `{question}` as part of one argument, with no shell. Must hold `{question}`. See [An agent run as a script](scripts.md). | none |
| `service` | An agent inside a web service, as `http://host:port/path`. Each run is one `POST` there. `https` is not read in this version. See [An agent inside a web service](#an-agent-inside-a-web-service). | none |
| `start` | With `service`: the command that starts a test copy of the service, such as `"uvicorn shop.service:app --port {port}"`. Scrimmage starts the copy for the test and stops it after. It is split into words like `command`, with no shell. Must hold `{port}`, the free port Scrimmage picks, and `service` must then hold `{port}` as its port, on `127.0.0.1`, `[::1]` or `localhost`. Not with `control` or `control_tls`, which are for a service someone else started. See [A service Scrimmage starts](#a-service-scrimmage-starts). | none |
| `body` | With `service`: the run's body, sent as JSON. A table, an array or a text that holds `{question}` in a text value. Required with `service`. | none |
| `headers` | With `service`: the run's headers, as a table of text. A value may hold `${NAME}` and `{session}`, never `{question}`. | empty |
| `record_grace_s` | With `service`: how long a run's record stays open after its reply, for calls the service makes after it answers. 0 to 60. | 5 |
| `record_wait_s` | With `service`: how much longer the runner waits for a run's last lines after the grace. Above 0, up to 120. | 10 |
| `background_real` | With `service`: calls work outside any request, such as a telemetry exporter, may make for real while a test runs. A thread a request without a run left running after its call returned is outside any request too; work the request waits for, and its background tasks, are not. Outside any request, a process start is a write, listed as `process:<program>`, and so is a multiprocessing send, a process pool's task or a subinterpreter call, listed as `process:multiprocessing`; a pool's own threads and its shutdown are not. A request sent to an app in the same process, through httpx's `ASGITransport` or `WSGITransport` or Starlette's `TestClient`, is no write, and the app runs as its caller, so its own writes meet the same rules; a request to a `MockTransport`, or to a subclass of those transports, is a write. It is never read for a call inside a run, which `real` decides. | empty |
| `control` | With `service`: the `ip:port` the runner listens on, for a service in a container to reach, such as `"172.17.0.1:8766"`. An IP address, never a name, because `localhost` can mean `::1`. An address that listens on every interface, such as `0.0.0.0`, is refused. An address that is not loopback needs `control_tls`. See [A service in a container](docker.md#a-service-in-a-container). | the loopback address of `SCRIMMAGE_CONTROL_URL` |
| `control_tls` | With `service` and `control`: `{cert = "path", key = "path"}`, the certificate chain and private key the control server speaks TLS with, as paths from the folder of this file. `scrimmage cert` makes them. The key must be a plain file you own, mode `600`. A certificate that does not match the key, or is outside its dates, stops the test with exit 2. Allowed on a loopback `control`, so a team can try its certificate on one machine. | none |
| `children` | `true` fires faults in child processes too, and lets a child on another Python join if it has Scrimmage installed. Not with `service`. See [Faults in children](scripts.md#faults-in-children). | `false` |
| `tools` | The agent's own tools, as `module:function` import paths, or `module:Class.method` for a tool that is a method. Scrimmage swaps each one for a gate when your code imports its module. When the model sees a different name, write `{path = "package.tools:lookup_order", name = "lookup-order"}`; labels use the name the model sees, which for a method is its own name. See [Tools that are methods](#tools-that-are-methods). | empty |
| `real` | Calls that are safe to run for real when a test does not mock them, such as `"tool:check_inventory"`, `"tool:*"` for every tool, `"mcp:docs/search"` for a tool on an MCP server, `"hosted:mcp/orders"` for a hosted MCP server, `"process:python"` for a Python child that does not join, or a web write such as `"http:orders.acme.internal POST /refunds"`. Model calls are not listed: they are real unless a fault names them. | empty |
| `runs` | How many times each row runs. | 3 |
| `parallel` | How many runs go at the same time. | 4; 1 with `service` |
| `run_timeout` | Seconds one run may take before it is stopped and marked "timed out". It counts from when Scrimmage starts the program, so start-up time counts too. | 120 |
| `max_cost_usd` | The most the whole test may spend on model calls, in dollars. `"none"` turns the budget off, on purpose. | 5 |
| `model_hosts` | Extra hosts whose requests are model calls, such as a gateway, each with the format it speaks: `[{host = "llm-gateway.internal", provider = "openai"}]`. A host name only, with no port. | empty |
| `reserve_output_tokens` | The output tokens a budget reservation assumes when a request does not set `max_tokens`. A call that does not fit while others hold the budget waits for them; see [When calls in flight hold the budget](model-faults.md#when-calls-in-flight-hold-the-budget). | 4096 |
| `prices` | Prices for other models, as the table below. The key `""` prices a call that sends no model name. | empty |
| `answer` | Where the answer is in what the function returns, such as `"messages[-1].content"`, or in a service's JSON reply, such as `"reply"`. With `function` or `service`. Required when the function returns anything but text, as the whole value would hold tool results too. See [Agents on a framework](frameworks.md). | the returned text, or a service's whole reply |

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

Scrimmage imports only modules under your agent's own top-level packages, the ones `function` and `tools` name. For a script, that is the ones `tools` names.

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
Scrimmage gates the method on the class when the module loads, so every instance goes through the gate, whether it was made before or after.
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
| an attribute your code sets in `__init__` | the run stops: Scrimmage gates the class when the module loads, before any instance exists, so define it as a method |
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
| `recorded` | `true` marks a mock written from a recorded run, not by hand. Its only effect: if no run used it, the report gives a note, not a test mistake. `true` or `false`; `false` by default. |
| `args` | On a tool or MCP mock: the arguments it answers, exactly, as a table, such as `{order_id = "1234"}`. See [Mocks that match arguments](#mocks-that-match-arguments). A web mock has no `args`. |

See [Web requests](web-requests.md) and [MCP tools](mcp.md).

### Mocks that match arguments

A tool or MCP mock with `args` answers only a call whose arguments equal its `args` exactly.
The types must match too: `{order_id = "1234"}` answers `get_order("1234")`, not `get_order(1234)`, `get_order("5678")` or a call that also sends another argument.
There is no looser form: no subset, no ignored key and no pattern.
A call that does not match goes on to the next mock, and the first mock that matches wins.
Then it goes to the normal rule: it runs for real if its label is under `real`, and is refused if it is not.
So a mock never gives one order's data to a question about another.

```toml
[[test.mock]]
call    = "tool:get_order"
args    = {order_id = "1234"}
returns = {order_id = "1234", status = "shipped", delivered = "Oct 30"}
```

`scrimmage record` writes these for you, with `recorded = true`; see [Recording replies](recording.md).
You can write `args` by hand too.
It is the way to pin a tool that writes: a mock of `refund_order` with `args = {order_id = "1234"}` answers a refund of order 1234 and refuses a refund of order 5678, which never runs.
A mock with no `args` answers every call to its tool, as before, so on a tool that writes it hides every difference between calls.

What `args` must hold is the form the gate sees:

| Call | The arguments as `args` holds them |
|---|---|
| A plain or method tool | Bound by the signature, so the names are the parameters' names, whether the call was positional or not. |
| A `**kwargs` parameter | Nested under its own name: `{kwargs = {a = 1}}`. |
| A default the call did not send | Absent. Defaults are not filled in. |
| A LangChain tool called positionally | `{args = [...]}`. |
| An argument a framework fills in and never shows the model | Not held, so not compared. |

Each value must be text, a number, `true` or `false`, a list or a table.
A TOML date, time or datetime is a mistake, exit code 2, because the gate can never hold one and the mock could never match.

A call that had a mock on its label and matched none is a miss.
The report says so, with the arguments the call sent and the mock that came nearest; see [Mocks that miss](#mocks-that-miss).

A mock with no `args` above a mock with `args` on the same label is a mistake, exit code 2: the first answers every call, so the second never does.
Put the mock with `args` first and the catch-all last.

`real = ["tool:*"]` does not cover a call that misses a mock with `args`.
Such a call is refused unless its label is under `real` by name, such as `"tool:get_order"`, because `tool:*` includes tools that write.
`scrimmage record` on a file with these mocks follows the same rule.

An unused mock with `args` is a note when its tool was called with other arguments.
An unused recorded mock is a note too, because a later run that does not repeat a recorded call leaves it unused by design.
An unused hand-written web or MCP mock is still a mistake, as its label may be misspelled.

#### Mocks that miss

A miss on a tool under `real` by name is a warning, and the exit code does not change: the team said the call is safe, and the result is a real observation of the agent.
A miss on a tool that is not under `real` by name is a refusal, as before, and it now names the arguments.
A web read that misses runs for real, and a web write that misses is refused, as before.

```text
warning: tool:get_stock with {"sku": "SKU-42"} matched no mock and ran for real, as it is under [agent] real; nearest mock (heuristic: fewest differing arguments): {"sku": "42"}, differs at sku; values cleaned by a heuristic (order-status baseline, 1 of 1 runs)
```

The warning shows values only for the arguments that differ.
Every other argument shows its name and type, such as `<str>`.
The nearest mock is a guess, chosen for display only: it never decides a match.
Values are cleaned by a heuristic, so a secret that fits no pattern and sits under an ordinary key name may still print.
The same misses are in `--json`, in `mock_misses`; see [JSON output](json-output.md).
A row that had a miss says so beside its broken rules, and `mock_miss_count` is on every row and run.

To act on a miss:

| You want | Do |
|---|---|
| The flip gone at its source | Fix the tool description or the prompt, so the model sends one form. |
| Both values answered | Add a second mock for the other value. |
| One reply for any value | Remove `args`. |
| A miss to stop the run | Remove the tool from `real`. |

## [[test.fault]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to fail, such as `"tool:get_order"`, `"mcp:docs/search"`, `"model"` or `"http:inventory.acme.internal"`. Required. |
| `do` | The fault: `"timeout"`; on a tool or an MCP tool `"error"`; on an MCP tool `"empty"`; on a model call or a web request `"status 429"` or any status from 400 to 599. On a tool, an MCP tool or a web request, also `"append"` and `"replace"`, which change what a call returns, and `"slow 8s"`, which delays it by that many seconds; the call itself still happens. See [Changing a result](changing-results.md). On a chat model call, `"replace"` with a `model`, which changes the reply's text, and `"slow 8s"`; not `"append"`. See [A guard model](model-faults.md#a-guard-model). Required. |
| `raises` | For `"error"` on a Python tool: the error to raise, a built-in such as `"ConnectionError"`, or `package.module:Class` from your agent's packages. Optional. |
| `text` | With `"append"` and `"replace"`: the text to add, or to put in place. Up to 64 KiB. Required with those two, refused with any other fault. |
| `find` | With `"replace"`: replace only this text, wherever it is, compared exactly. Without it the whole result is replaced. On a model call, it is looked for in the text of each choice of the reply. Optional. |
| `when` | When it fires: `"always"`, the default; `"first N"`, the first N attempts; `"call N"`, the Nth attempt alone; or on a model call `"requests N"`, the first N requests and every retry of each. `"once"` and a bare number are refused: write `"first 1"` or `"call N"`. |
| `method` | On a web request: fail only this method. |
| `path` | On a web request: fail only this exact path, with no query string. |
| `model` | On a model call: fail only calls to this model, such as `"gpt-4.1"`. The whole name your code sends, compared exactly. Required with `"replace"`. See [Failing one model](model-faults.md#failing-one-model). |
| `expect` | Replaces the test's for this row. A fault that fires on every call needs this or `forbid`, except `append` and `replace` with `find`, where the agent still gets the data; with `first N` or `call N` it can use the test's. See [Judging an answer](judging.md). |
| `forbid` | Replaces the test's for this row. `[]` drops the test's list. |
| `pass_rate` | Replaces the test's for this row. |

## Checking the file in your editor

Scrimmage ships a JSON Schema of this file, and `scrimmage schema` prints the one that matches your installed version:

```bash
scrimmage schema > scrimmage.schema.json
```

Then make this the first line of `scrimmage.toml`:

```
#:schema ./scrimmage.schema.json
```

Taplo, the TOML language server behind VS Code's Even Better TOML extension, reads that comment and checks each key, type, label, schedule and fault word as you type.
Rust and Python teams use it the same way for `Cargo.toml` and `pyproject.toml`.
Scrimmage reads the line as a comment, so it changes nothing.

The schema checks the shape of the file only.
It cannot check what needs the whole file, your folder or your machine: a tool name against `tools`, a mock on a model call, a `type` outside your packages, a program on your `PATH`, or a test name used twice.
The schema cannot see your environment, so it cannot tell a `${NAME}` that is not set; Scrimmage stops on it.
It cannot tell a whole number written as `2.0` from `2`, as JSON Schema counts both as an integer, so Taplo and other editors accept `runs = 2.0`; Scrimmage rejects it.
An editor that finds nothing wrong is a good sign. `scrimmage check` decides; see [Commands](cli.md#scrimmage-check).

## Placeholders

`${NAME}` in any text value of the file is filled in from the environment when the file is read.
`NAME` is a letter or `_`, then letters, digits or `_`.
A name that is not set stops the test with exit 2, naming the table, the key and the name, such as `[agent] headers Authorization: ${TEST_TOKEN} is not set in the environment`.
A name set to an empty value is read as empty.
`$${` is a literal `${`, the escape Docker Compose uses too.
Unlike Compose, `$$` alone is not an escape: it stays two dollar signs, so `$$${HOME}` is a `$` and then a literal `${HOME}`.
This is the form `scrimmage record` writes for a recorded `$${`, so it reads back as it was.
Keys are never read.

A filled-in value is text as it is: a value that holds `{question}` or `${OTHER}` is never read again.
In `command` the line is split into arguments first, and each argument is filled in after, so a value with a space stays one argument.

Each filled-in value of 8 characters or more is removed from every record, side-folder item, summary, report and `result.json`, as written, JSON-escaped, URL-encoded and in base64.
A value under 8 characters is not removed, as it would match too much other text: keep secrets longer than that.
A row's hash in `result.json` is taken over the file as written, `${NAME}` and all, so it never covers a value from the environment, and a rotated token is not a changed test.

`{question}` and `{session}` are read in `body` and `headers` only.
`{question}` is the test's question; a header cannot hold it.
`{session}` is `scrimmage-` and 16 random hex digits, new for every run, and never the run's name.
In those two fields `{{` is `{` and `}}` is `}`, and any other `{...}` is a test mistake.
Each text is read once, so a question that holds `{session}` reaches the service as written.
Anywhere else braces are kept as written, so a mock's text such as `{"a": {"b": 1}}` is sent as it is.
`command` reads `{question}` as before, with no `{{` and no `{session}`, so `awk '{print $1}'` still works.

| Written in `body` | Sent |
|---|---|
| `"{question}"` | the question |
| `"id {{x}} {session}"` | `id {x} scrimmage-0f3a...` |
| `"Bearer ${TEST_TOKEN}"` | `Bearer` and the variable's value |
| `"$${HOME}"` | `${HOME}` |
| `"{other}"` | exit 2: not a placeholder |

## An agent inside a web service

`service` names the address of an agent that runs inside a web service, such as a FastAPI app under uvicorn.
How to start the service, and what happens before and during each run, is on [An agent inside a web service](services.md).
Each run is one `POST` to that address, with `body` as JSON and `headers` added.
`scrimmage check` prints the two lines the service's environment needs, `SCRIMMAGE_TOOLS` and `SCRIMMAGE_ALLOW_IMPORTS`, built from the file.
`scrimmage record` and `scrimmage init --probe` do not work with a service in this version: record or probe the same agent as a function.

```toml
[agent]
service = "http://127.0.0.1:8765/chat"
body    = {message = "{question}", session = "{session}"}
answer  = "reply"
headers = {Authorization = "Bearer ${TEST_TOKEN}"}
```

### A service Scrimmage starts

With `start`, Scrimmage starts its own test copy of the service, and stops it when the test ends.
The team starts nothing by hand.

```toml
[agent]
start   = "uvicorn acme.service:app --port {port} --loop asyncio"
service = "http://127.0.0.1:{port}/chat"
body    = {message = "{question}"}
```

`{port}` is a free port on this machine, picked for each test.
It is filled in the file's own text in `start` and in `service`; a `${NAME}` value that holds `{port}` is sent as written.
There is no `{{` escape for it: in `start`, `{{port}}` is `{`, the port and `}`, and in `service` it is refused.
`start` must hold it at least once, and `service` must hold it once, as its port.
The copy runs on this machine, so the host in `service` must be `127.0.0.1`, `[::1]` or `localhost`, written in the file, not from a `${NAME}` value.
Use `127.0.0.1`: uvicorn listens there by default, and `localhost` can name `::1` first, which costs a refused connection before the client tries `127.0.0.1`.
`control` and `control_tls` are refused beside `start`: they are for a service someone else started, such as one in a container.
Without `start`, `{port}` is not read, and `service` must name a port as digits.

The first word of `start` is a program found on `PATH`, or a path from the folder of this file.
A program it cannot find stops `scrimmage check` with exit 2.
Activate the virtual environment that has it, or write `python -m uvicorn`.
A Python flag such as `-I`, `-S` or `-E` keeps Python from running Scrimmage's startup file, so it stops `scrimmage check` with exit 2 too, as in `command`.
`${NAME}` in `start` is filled in after the split, as in `command`, and each value of 8 characters or more is removed from what is recorded.

## Coming in later versions

These are part of Scrimmage's design but not of this version.
Using one now stops the test: it is rejected as unknown.

- Settings: `records`.
- Labels: `auth:`, and model providers other than OpenAI.
- Faults: `empty` on a tool or a web request, `refused`, `reset`, `dns`, `bad body`, `auth error`, `context too long`, `append` on a model call, the other model reply faults, and `status` on a tool.
- Fault options: `retry_after`.
- Mocks: `returns_from`.
