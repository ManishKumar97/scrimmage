# Configuration: faultline.toml

One `faultline.toml` describes one agent and its tests.
Faultline names the table and key of every bad value, such as `[agent] run_timeout: expected a number of seconds, got 'abc'`.
A setting that a later version will build is rejected with "not supported yet", never silently ignored.

## [agent]

| Setting | What it does | Default |
|---|---|---|
| `function` | The agent, as `module:function`. Faultline calls it with the question and waits for it if it is async. Required. | none |
| `tools` | The agent's own tools, as `module:function` import paths. Faultline swaps each one for a gate when your code imports its module. | empty |
| `real` | Tool labels that are safe to run for real when a test does not mock them, such as `"tool:check_inventory"`, or `"tool:*"` for every tool. | empty |
| `runs` | How many times each row runs. | 3 |
| `parallel` | How many runs go at the same time. | 4 |
| `run_timeout` | Seconds one run may take before it is stopped and marked "timed out". | 120 |

Faultline imports only modules under your agent's own top-level packages, the ones `function` and `tools` name, such as `acme.`.

## [[test]]

| Setting | What it does |
|---|---|
| `name` | A name for the test, unique in the file. Required. |
| `question` | The question the agent gets. Required. |
| `expect` | A list of text the answer should contain. Required; used for judging in a later version. |

## [[test.mock]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to mock, such as `"tool:get_order"`. Required. |
| `returns` | The value to return, in TOML: text, a number, true or false, a list or a table. Required. Dates and times are not allowed; write them as text. |
| `type` | The type to build the value into, as `module:Class`. Optional; read from the tool's return annotation when left out. |

## [[test.fault]]

| Setting | What it does |
|---|---|
| `call` | The label of the call to fail. Required. |
| `do` | The fault. This version has `"timeout"`. Required. |
| `when` | When it fires. This version has `"always"`, the default. |

## Not supported yet

These are part of Faultline's design and come in later versions.
Using one now stops the test with "not supported yet".

- Agent shapes: `command` (a script) and `service` (a web service).
- Settings: `max_cost_usd`, `model_hosts`, `answer`, `body`, `headers`, `control`, `control_tls`, `reserve_output_tokens`, `[agent.prices]`, `records`, `record_grace_s`, `record_wait_s`.
- Labels: `model`, `mcp:`, `http:`, `auth:`, `hosted:`.
- Faults: `status 429` and other statuses, `error`, `slow`, `empty`, `refused`, `reset`, `dns`, `bad body`, `auth error`, `context too long`, `append`, `replace`, and the model reply faults.
- Schedules: `once`, `call N`, `first N`, `requests N`.
- Fault options: `raises`, `retry_after`, `append`, `replace`, and `method` and `path` to narrow a web-request fault.
- Mocks: `returns_from`, and web-request mocks (`method`, `path`, `status`, `json`, `text`).
- Placeholders: `${VAR}`.
