# JSON output

`scrimmage test --json`, `scrimmage check --json`, `scrimmage record --json` and `scrimmage show --json` print one JSON document on standard output and nothing else.
They are for a coding agent or a CI step that reads the result, not the table.
Read the document with one `json.loads` of standard output, then act on its fields and their stable values, never on message text.

The document is printed when the command ends.
A test can take minutes, so nothing is printed before then.

Its shape is the JSON Schema `scrimmage/schemas/cli.v1.json`, which ships in the package.
The schema is open: a field it does not list, and a value it does not list for a field that lists its values, both fit, so any version 1 copy of it reads any version 1 document.

## The envelope

Every command's document starts with the same fields:

```json
{
  "v": 1,
  "scrimmage": "0.1.0rc2",
  "command": "test",
  "exit": 1,
  "status": "failed",
  "config": "scrimmage.toml",
  "mistakes": [{"message": "order-status fault 1: the fault never fired in any run, so the row tested nothing"}],
  "notes": [],
  "warnings": [],
  "infra": []
}
```

| Field | Meaning |
|---|---|
| `v` | The version of this format, 1. |
| `scrimmage` | The installed Scrimmage version, so a document can be traced to the release that made it. `unknown` when Scrimmage runs with no package metadata, such as from a source tree. |
| `command` | `test`, `check`, `record`, `init` or `show`. |
| `exit` | The process's exit code. Always equal to it. |
| `status` | A word for the exit code, per command; see below. |
| `config` | The file read, as given on the command line. |
| `mistakes` | One item per line the text prints after `test mistake:`, or per problem in the file. |
| `notes` | One item per line the text prints after `note:`. |
| `warnings` | One item per line the text prints after `warning:`. With `real = ["tool:*"]` the first one says that every tool that is not mocked runs for real; the text prints it above the table. |
| `infra` | One item per line the text prints after `infrastructure error:`, and the reason Scrimmage itself stopped, such as `stopped by SIGTERM`. When a run failed by itself, it points at the runs whose `outcome` is `infrastructure error`, in `run_details`. |

Each item is an object with one field, `message`, the line's text: `{"message": "..."}`.
It is an object so a later version can add a field to it, such as a code, without changing its type.
The text of a message can change in any release.

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `test` | `passed` | `failed` | `mistake` | `infra` |
| `check` | `valid` | | `mistake` | `infra` |
| `record` | `complete` | `incomplete` | `mistake` | `infra` |
| `init` | `written` | | `mistake` | `infra` |
| `show` | `shown` | | `mistake` | `infra` |

## scrimmage test --json

```json
{
  "v": 1, "scrimmage": "0.1.0rc2", "command": "test", "exit": 1, "status": "failed", "config": "scrimmage.toml",
  "records": "/home/acme/bot/scrimmage-runs/20261107T101500Z-a1b2c3",
  "tests": 1,
  "rows": [
    {
      "test": "order-status",
      "row": "fault 1",
      "fault": {"call": "tool:get_order", "do": "timeout", "when": "always", "model": null, "method": null, "path": null, "raises": null, "text": null, "find": null, "retry_after": null},
      "runs": 3,
      "fired": 3,
      "done": 3,
      "passed": 1,
      "judged": 3,
      "rate": 0.3333,
      "range": [0.0615, 0.7923],
      "pass_rate": 1.0,
      "tools": 1.0,
      "cost_usd": 0.00042,
      "result": "fail",
      "broken": [{"kind": "forbid", "text": "forbid \"Oct 30\" found", "runs": ["run-3f9a1c40b2e1", "run-8e21d07a9c33"]}],
      "run_details": [
        {"run": "run-3f9a1c40b2e1", "outcome": "answered", "fault_fired": true, "judged": "fail", "left_out": null, "cost_usd": 0.00014, "answer_preview": "Your order shipped on Oct 30."}
      ]
    }
  ],
  "cost_usd": 0.00042,
  "budget_usd": 5.0,
  "stopped_by": null,
  "mistakes": [], "notes": [], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `records` | The folder that holds this test's records, as the text's first line names it: under `scrimmage-runs/` beside the file, or under `--records DIR`. `null` when the file did not load, so no folder was made. |
| `tests` | How many `[[test]]` tables the file has. |
| `rows` | One object per table row, in the table's order. Empty when the file did not load, when a signal stopped the test before any run ended, or when Scrimmage itself failed. After a signal, it holds the rows of the runs that had ended. A test a refusal stopped, such as for the budget, still has every row, and the rows that did not run have `runs` 0. |
| `cost_usd` | What the model calls cost, in dollars. |
| `budget_usd` | The cost cap the test ran under: `max_cost_usd`, or the lower `--max-cost-usd`, or `null` when the budget is off. |
| `stopped_by` | The reason a refusal stopped the whole test, such as `budget`, or `null`. `interrupted` when SIGINT or SIGTERM stopped it after some runs had ended: the rows count only those runs, and `result.json` is written. |
| `mock_misses` | The calls that had a mock on their label and matched none, one object per distinct call, below. Absent in `scrimmage record --json`. At most 200, real calls first. |
| `mock_misses_left_out` | How many more misses than the 200 listed. |
| `runs_sent` | With a service: the requests the runner sent to it, the setup request and every run. Absent for a function or a script. |
| `claims_received` | With a service: the claims the control server answered. Far below `runs_sent` means no service reached the control server. |
| `control_cut` | With a service: the control server's connections that the read or whole-request timeouts cut. Near zero in a healthy test. |
| `control_ping` | With a service: what the service's signed ping to the control server found before the first run: `reached`, `tls`, `error`, `detail`, `rtt_ms`, `runner_id` and `url`, as the [startup report](schemas.md) gives them. Present in every finished service test. A test that stops at the probe or at the ping has no `control_ping`: the reason is in `infra`. |
| `control_tls_failed` | With a service: connections that failed TLS, counted by OpenSSL's reason, such as `unknown ca`. Always present for a service, and empty without `control_tls`. |
| `result_file` | The `result.json` this test wrote into `records`: the same document, but for this field, which the file leaves out, and `compare`, which is always `null` in the file. `null` when it was not written: a write failed, or the test did not finish. A test a signal stopped after some runs had ended writes it, with `stopped_by` set to `interrupted`. See [Reading a finished test](show.md). |
| `compare` | `null`, or with `--compare` the comparison with the earlier test, below. |

Each object in `mock_misses`:

| Field | Meaning |
|---|---|
| `test`, `row`, `call` | Where the miss was, and the call's label. |
| `reason` | `arguments`: a tool or MCP call, no mock's `args` equal. `request`: a web call, no mock names its method and path. `no_mock`: a refused tool call with no mock of its own, in a test that has mocks with `args`. `unreadable`: the evidence could not be built; the decision stands. |
| `action` | `real`: the call ran for real. `refused`: it was refused. |
| `method`, `path` | For a web call, what it sent. `null` otherwise. |
| `seen` | The arguments the call sent, as display text: values for the arguments that differ, the name and type for the others. Cleaned by a heuristic. Do not parse it. |
| `nearest` | The mock that came nearest, chosen for display only by fewest differing arguments, or for a web call by most path segments alike. `null` for `no_mock`. |
| `nearest.mock` | The mock's place in the test, counting from 1. |
| `nearest.args` | Its arguments as display text, cleaned the same way. Do not parse it. |
| `nearest.method`, `nearest.path` | For a web mock. |
| `nearest.differs` | The arguments that differ, or for a web call `method` and `path`. |
| `differs_left_out` | In `nearest`: how many more differ than `differs` lists. |
| `runs` | The distinct runs that made this miss. |
| `run` | The first of them. |

A miss changes no `status` or `exit`.

Each row's keys are the table's column names, with `cost_usd` for `cost`:

| Field | Meaning |
|---|---|
| `test` | The test's name. |
| `row` | `baseline`, or `fault N`. |
| `fault` | The row's fault: `call`, `do`, `when`, and `model`, `method`, `path`, `raises`, `text`, `find`, `retry_after`, `expect`, `forbid` and `pass_rate`, each `null` unless the fault sets it. `expect`, `forbid` and `pass_rate` are the fault's own rules, which judge the row in place of the test's. `null` on a baseline. |
| `runs` | Runs that ran. |
| `fired` | Runs in which the fault fired. `null` on a baseline. |
| `done` | Runs that ended with an answer. |
| `passed` | Judged runs that passed. |
| `judged` | Runs that were judged. |
| `rate` | `passed` over `judged`, rounded to 4 places. `null` when no run was judged, or when `result` is `baseline failed`. |
| `range` | The 95% Wilson interval of the rate, as two fractions rounded to 4 places. `null` when `rate` is. |
| `pass_rate` | The share of judged runs the row needs to pass. |
| `tools` | Tool calls per run, on average. |
| `cost_usd` | What this row's model calls cost. |
| `result` | The row's result. `baseline failed` on a fault row whose test's baseline did not pass; see [Judging](judging.md#a-fault-row-beside-a-failing-baseline). |
| `broken` | Each rule a run broke: its `kind`, its `text` and every `runs` name that broke it, the most broken first. |
| `mock_miss_count` | The calls in this row's runs that had a mock on their label and matched none. |
| `run_details` | One object per run, below. |
| `test_sha256` | A hash of what the row was tested by: the question, the rules that judge the row, the mocks, `run_timeout`, `real` and `tools`. Not the agent's code. A comparison uses it to tell a changed test from a changed agent. |
| `test_sha256_v` | The format of `test_sha256`, 1. A comparison across two formats cannot tell whether the test changed. |
| `control_busy` | The sum of the row's runs' `control_busy`, or `null` when no run reported one. |
| `control_p99_ms` | The largest of the row's runs' `control_p99_ms`, or `null`. |

Each run in `run_details`:

| Field | Meaning |
|---|---|
| `run` | The run's name, which is also its record's name in `records`. |
| `outcome` | How the run ended. |
| `fault_fired` | Whether the fault fired. `null` on a baseline. |
| `judged` | How the run counts. |
| `left_out` | Why a run was left out of the rate, such as `the fault did not fire`, or `null`. |
| `cost_usd` | What this run's model calls cost. |
| `answer_preview` | The answer with secrets removed, cut at 200 characters, as a record's preview is. The full answer is in the record. `null` with no answer. |
| `mock_miss_count` | This run's calls that had a mock on their label and matched none. |
| `control_busy` | Control requests the process that served this run refused itself, for no free slot of its 8, from the run's start to its close. It describes the process: two runs that overlap in one worker count the same requests. |
| `control_p99_ms` | The 99th percentile of that process's control round trips in the same window, in milliseconds, or `null`. A value rising toward 80 ms is the warning before runs end as infrastructure errors with `plan-unavailable`. |

A file that does not load gives the envelope with `rows: []`, `records: null` and each problem in `mistakes`, with exit 2.
With `--json`, once Scrimmage has started, Ctrl-C or SIGTERM while Scrimmage loads, while the file is read or while runs go stops the command with `status` `infra`, exit 3, `rows: []` and the reason in `infra`, such as `stopped by SIGTERM`.
Runs that were going are stopped first. `records` names the records folder if the test had made one.
A failure of Scrimmage itself gives the same, with the error in `infra`.
Scrimmage has started from the moment its entry point runs, a few tens of milliseconds after Python does; a signal before that ends the process with no document.
Once the result is settled, a signal is ignored until the process exits, so its exit code always equals the document's `exit`.
SIGKILL cannot be caught, so it leaves no document.

## scrimmage check --json

```json
{
  "v": 1, "scrimmage": "0.1.0rc2", "command": "check", "exit": 0, "status": "valid", "config": "scrimmage.toml",
  "summary": {"tests": 2, "rows": 5, "runs": 15, "tools": 4, "budget_usd": 5.0, "real": ["tool:check_inventory"], "entry": {"kind": "function", "value": "acme.agent:answer"}},
  "mistakes": [], "notes": [], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `summary` | What the file holds: `tests`, `rows`, the `runs` a test would start, the `tools` listed, `budget_usd`, `real`, `entry`, `prices`, `service_tier` and `price_sources`. With `--runs N`, `runs` counts what `scrimmage record --runs N` would start: one baseline per test, N times. `null` when the file does not load. |
| `real` | In `summary`: the labels under `[agent] real`, as the file writes them. |
| `prices` | In `summary`: each model `[agent.prices]` prices, with its `input`, `output`, `cached_input`, `cache_write_30m`, `cache_write_12h`, `cache_write_5m` and `cache_write_1h` dollars per million tokens, `null` for a rate it does not set, and its `tiers` and its `long` rates, with `above`, if it has any. Any other model is priced from `scrimmage-runs/prices.toml` if there is one, and from the shipped price file otherwise. A price of 0 makes that model's calls free against the budget. |
| `service_tier` | In `summary`: `[agent] service_tier`, the tier a request that names none is held at, or `null`. |
| `price_sources` | In `summary`: where prices come from, in order of authority. Each has a `source`, which is `[agent.prices]`, `scrimmage-runs/prices.toml` or the shipped price file, the `provider` whose section of the file it is, its `as_of` date, and the number of `models` it prices. A file with sections for two providers is listed twice. `provider` and `as_of` are `null` for `[agent.prices]`, which is flat. |
| `entry` | In `summary`: the entry point a test would start, its `kind`, `function` or `command`, and its `value`, the function or the command line. |
| `service` | Only with `scrimmage check --service`, and only when the service answered as a gated one: what it reported. Absent otherwise. |
| `url` | In `service`: the service's address, as the file writes it. |
| `gates` | In `service`: the entry gates installed in the process that answered, such as `asgi`. |
| `pid` | In `service`: the process that answered. |
| `sdk` | In `service`: the version of `scrimmage` in the service. A test runs only when it equals `runner_sdk`. |
| `runner_sdk` | In `service`: the version of `scrimmage` in the command. |
| `python` | In `service`: the service's Python version string. |
| `executable` | In `service`: the service's Python executable. |
| `tools` | In `service`: the tools its gates swap. |
| `tools_match` | In `service`: whether they are exactly the tools the file lists. |
| `control` | In `service`: the service's ping to the control server, as the [startup report](schemas.md) holds it. |
| `notices` | In `service`: the notices the service held when it answered. |

## scrimmage record --json

```json
{
  "v": 1, "scrimmage": "0.1.0rc2", "command": "record", "exit": 1, "status": "incomplete", "config": "scrimmage.toml",
  "out": "scrimmage.recorded.toml",
  "records": "scrimmage-runs/20261107T101500Z-a1b2c3",
  "cost_usd": 0.0012,
  "budget_usd": 5.0,
  "numbers": {"calls_seen": 4, "calls_written": 3, "runs": 1, "runs_recorded": 1, "argument_sets_seen": 1, "argument_sets_written": 1},
  "tests": [
    {
      "test": "order-status",
      "run": "run-3f9a1c40b2e1",
      "outcome": "answered",
      "runs": [{"run": "run-3f9a1c40b2e1", "outcome": "answered", "calls_seen": 4, "calls_written": 3, "cost_usd": 0.0012}],
      "not_started": 0,
      "written": [
        {"call": "http:inventory.acme.internal", "method": "GET", "path": "/stock/SKU-42", "calls": 1, "removed": []},
        {"call": "tool:get_user", "method": null, "path": null, "calls": 1, "removed": [{"key": "access_token", "rule": "key_name"}]}
      ],
      "kept": [{"call": "tool:refund_order", "mock": 1}],
      "skipped": [{"call": "http:sts.amazonaws.com", "method": "POST", "path": "/", "code": "credential", "calls": 1}],
      "manual": [
        {"call": "tool:get_invoice", "code": "unsavable", "method": null, "path": null, "calls": 1, "message": "returns: a bytes, which a mock cannot hold", "instead": null}
      ],
      "argument_sets": [{"call": "tool:get_user", "seen": 1, "written": 1, "sets": [{"runs": 1, "mock": 2, "code": null}]}]
    }
  ],
  "mistakes": [], "notes": [], "warnings": [{"message": "the mocks hold replies from real systems, which can hold customer data; check them before you commit them"}], "infra": []
}
```

| Field | Meaning |
|---|---|
| `out` | The file written, whole. `null` means nothing was written. The reason is in `mistakes` or `infra`, or, for a record with no mocks that keeps an earlier file, in `notes`, and that alone does not change the exit code, so it can be 0 and `complete`. |
| `records` | The folder that holds this record's run, or `null` when no run started. |
| `cost_usd` | What the model calls cost, in dollars. |
| `budget_usd` | The cost cap the record ran under: `max_cost_usd`, or the lower `--max-cost-usd`. `null` when the budget is off or no run started. |
| `stopped_by` | In a record: the reason a refusal stopped it, such as `budget`, as in `scrimmage test`, or `null`. A stop on the budget writes nothing. |
| `numbers` | `calls_seen`, the real tool, MCP and web calls that no other call made, less the credential requests. `calls_written`, those whose level was written whole. |
| `calls_seen`, `calls_written` | See `numbers`. Over every run of every test. |
| `runs` | In `numbers`: the runs of each test that `--runs` asked for. In a test: one object per run, in the order the runs were submitted, with `run`, `outcome`, `calls_seen`, `calls_written` and `cost_usd`. In a written mock: how many runs saw its key. |
| `runs_recorded` | In `numbers`: the runs whose calls were read, in tests with nothing wrong. |
| `argument_sets_seen`, `argument_sets_written` | In `numbers`: the distinct tool and MCP calls, by label and arguments, that ran for real, and the ones with a mock of their own. |
| `not_started` | In a test: the runs of the number asked for that never started, for any cause. |
| `argument_sets` | In a test: for each tool or MCP label that ran for real, `call`, `seen` (its distinct sets of arguments), `written` (those with a mock) and `sets`. Each set has `runs` (how many runs sent it), `mock` (its place among the output test's mocks from 1, or `null`) and `code` (the code it is listed with, or `null`). No argument value is in it, as a document reaches CI logs. The values are in the file's comments and in the text. |
| `tests` | One object per test that ran. |
| `test` | The test's name. |
| `run` | The run's name, also its record's name in `records`. |
| `outcome` | How the test's runs ended: `answered` if every run answered, else the outcome of the first run that did not. Its `run` is that run's name, or the first run's. |
| `written` | Each mock written: `call`, `method` and `path` (`null` for a tool or MCP call), `calls` (how many calls this mock stands for) and `removed` (each secret taken out: its `key`, `null` for a pattern match in plain text, and the `rule`, `pattern` or `key_name`). |
| `kept` | Each team mock used: its `call`, and `mock`, its place in the test from 1. |
| `skipped` | Each call skipped, with its `code` (below), `calls`, and `method` and `path` for a web request. |
| `manual` | Each call that could not be written: its `call`, `code` (below), `message`, `calls`, `method` and `path`, and `instead`. |
| `instead` | The mock written in this call's place, such as the mock of the tool that made it, so it needs nothing from you. `null` when you must write it. |
| `mocks_text` | Only when the file's layout could not take the mocks: the mocks, for you to copy in. `out` is then `null`. |

The diff is in the text output only: compare `config` with `out` to see it.
With `--runs` above 1, the line the text form prints before the first run, how many runs and what each calls for real, is the first of `notes`, so standard error stays empty.

## scrimmage init --json

```json
{
  "v": 1, "scrimmage": "0.1.0rc2", "command": "init", "exit": 0, "status": "written", "config": "scrimmage.toml",
  "out": "scrimmage.toml",
  "entry": {"kind": "function", "value": "support.agent:answer", "guess": false, "candidates": [{"value": "support.agent:answer", "why": "one argument, named question, in a module that imports openai"}]},
  "probe": {"ran": true, "function": "support.agent:answer", "outcome": "stopped at POST /v1/chat/completions on gateway.acme.internal, refused before it was sent"},
  "numbers": {"tools_offered": 4, "tools_mapped": 4},
  "tools": [{"path": "support.desk:Desk.lookup_ticket", "name": "lookup_ticket", "found_by": "probe"}],
  "adapter_tools": [],
  "unresolved": [],
  "model_hosts": [{"host": "gateway.acme.internal", "provider": "openai", "found_by": "probe"}],
  "mistakes": [], "notes": [{"message": "the probe ran your code up to its first model request; ..."}], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `out` | The file written. A non-null value means it was written, whole. `null` means nothing was written, and `mistakes` or `infra` says why. |
| `entry` | The entry point: `kind`, `function` or `command`, its `value`, `guess`, true when init chose it by reading the code and false when `--probe` named it, and every `candidates` item with its `value` and `why`, best first. `null` when none was found. |
| `probe` | `ran`, whether a probe ran, `function`, the function `--probe` named, and `outcome`, what ended it, in words. `{"ran": false, "function": null, "outcome": null}` without `--probe`. |
| `numbers` | `tools_offered`, the names the probe's model request offered, and `tools_mapped`, those written as one path. `null` when no model request offered a tool that could be read. |
| `tools` | Each tool written: its import `path`, the `name` the model sees, and `found_by`. |
| `adapter_tools` | Tools a framework's adapter sees, which need no `tools` list: `name` and `found_by`. |
| `unresolved` | Each name that could not be mapped to one path: `name`, `candidates`, every path it could be, and `problem`. |
| `model_hosts` | Each host to name under `model_hosts`: `host`, `provider` and `found_by`. The built-in OpenAI host is left out. |

## scrimmage show --json

```json
{
  "v": 1, "scrimmage": "0.1.0rc2", "command": "show", "exit": 0, "status": "shown",
  "config": "scrimmage-runs/20261111T101500Z-a1b2c3",
  "records": "scrimmage-runs/20261111T101500Z-a1b2c3",
  "result": {"v": 1, "command": "test", "rows": ["..."]},
  "runs": [
    {
      "run": "run-3f9a1c40b2e1", "test": "order-status", "row": "fault 1",
      "outcome": "answered", "judged": "fail", "left_out": null,
      "broken": [{"kind": "forbid", "text": "forbid \"Oct 30\" found"}],
      "answer_preview": "Your order shipped on Oct 30.",
      "lines": [
        {"ms": 412, "call": "tool:get_order", "kind": "tool", "action": "fault", "came_back": "TimeoutError: injected (injected)", "depth": 0, "mock_miss": false}
      ],
      "lines_left_out": 0, "log_tail": null,
      "suggestions": [
        {
          "code": "forbid_after_fault",
          "text": "suggestion (a heuristic): the answer holds text the test forbids, after an injected failure in tool:get_order. Scrimmage cannot see where that text came from; look at what your code gives the model when tool:get_order fails.",
          "heuristic": true,
          "evidence": [{"seq": 2, "proc": {"host": "h", "pid": 100, "started": "2026-11-11T10:15:00+00:00"}}]
        }
      ]
    }
  ],
  "compare": null,
  "mistakes": [], "notes": [], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `config` | `TARGET` as given, or the folder chosen when there was none. |
| `records` | The folder read. `null` when none was found. |
| `result` | The folder's `result.json`, which is a `scrimmage test --json` document. `null` when the folder has none. |
| `runs` | The runs shown: the first failing run of each failing row, or the one run `--run` named. Empty when there is nothing to show. |
| `compare` | `null`, or with `--compare` the comparison, below. |

Each run in `runs`:

| Field | Meaning |
|---|---|
| `run`, `test`, `row` | The run, and the test and row its record names. |
| `outcome`, `judged`, `left_out` | As in `run_details`. All `null` when the run's record has no summary line, which a note says. |
| `broken` | The rules the run broke, each with its `kind` and `text`. |
| `answer_preview` | The answer with secrets removed, cut at 200 characters. `null` with no answer. |
| `lines` | The run's calls in the order they started, at most 200. |
| `lines_left_out` | How many more calls the record holds. |
| `log_tail` | For a run that crashed or timed out, the `path` of its log and the last `line` of it, redacted and cut at 200 characters. Otherwise `null`. |
| `suggestions` | Heuristic suggestions, below. Empty for a pass, and when no rule fits. |

Each item in `suggestions`:

| Field | Meaning |
|---|---|
| `code` | A stable name for the rule that fired. Read this, never `text`: the wording can change. |
| `text` | The suggestion in words. It starts `suggestion (a heuristic):`. |
| `heuristic` | Always `true`. A suggestion is a rule over the record, not a finding. |
| `evidence` | What the rule read. A record line is `seq` and `proc`, the line's sequence number and the process that wrote it. `{"summary": true}` means the rule read the run's summary line, such as its outcome, and no one call. |

The main suggestion comes first, then at most one about a mock that missed.
See [Reading a finished test](show.md#suggestions).

Each item in `lines`:

| Field | Meaning |
|---|---|
| `ms` | Milliseconds from the run's first call. |
| `call` | The call's label. |
| `kind` | The kind of call, such as `model`, `tool` or `http`. |
| `action` | What the gate did: `real`, `mock`, `fault` or `refused`. |
| `came_back` | The call's error, or the start of its result, with secrets removed, cut at 80 characters. |
| `depth` | 0, or 1 and more for a call made inside another, such as a web request inside a tool. |
| `mock_miss` | `true` when the call had a mock on its label and matched none. |

Exit 0 is `shown`, whatever the test's own result was.
Exit 2 is `mistake`: no folder, no such run, a bad run id, a folder with no run records, a record or result of a version Scrimmage does not read, or a file it refuses to open because it is a link, another user's, not a plain file or too large.
Exit 3 is `infra`.
`show`'s output can hold customer data from the records; see [Reading a finished test](show.md#redaction-and-what-can-still-be-in-the-output).

### compare

`scrimmage test --compare EARLIER --json` and `scrimmage show --compare EARLIER --json` fill `compare`:

```json
{
  "earlier": "scrimmage-runs/20261110T090000Z-9f8e7d",
  "earlier_scrimmage": "0.1.0rc2",
  "rows_compared": 1,
  "rows": [
    {
      "test": "order-status", "fault": {"call": "tool:get_order", "do": "timeout", "when": "always"}, "n": 1,
      "row_before": "fault 1", "row_after": "fault 1",
      "before": {"passed": 0, "judged": 3, "rate": 0.0, "range": [0.0, 0.5615], "result": "fail"},
      "after": {"passed": 3, "judged": 3, "rate": 1.0, "range": [0.4385, 1.0], "result": "pass"},
      "difference": {"value": 1.0, "range": [0.2052, 1.0]},
      "change": "better", "result_change": "fail -> pass", "test_changed": false
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `earlier`, `earlier_scrimmage` | The `EARLIER` given, and the Scrimmage version that made it. |
| `rows_compared` | How many rows are listed, new and gone ones included. |
| `rows` | One object per row, the rows whose result went from `pass` to `fail` first. |
| `test`, `fault`, `n` | The row's key: its test, its fault with every `null` field left out, and its place among the test's rows with an identical fault. `fault` is `null` on a baseline. Names such as `fault 2` are not the key, because they are only a place. |
| `row_before`, `row_after` | The row's name on each side. `null` on the side that does not have it. |
| `before`, `after` | That side's `passed`, `judged`, `rate`, `range` and `result`. `null` when the row is on the other side only. |
| `difference` | After minus before as `value`, with a 95% interval as `range`, Newcombe's method 10 from the two Wilson ranges. `null` when the rows cannot be compared by rate. |
| `change` | `better`, `worse`, `unclear`, `no result`, `new` or `gone`; see [Reading a finished test](show.md#telling-two-rates-apart). Act on this field, not on the text. |
| `result_change` | The judge's result on each side, such as `pass -> fail`. `null` for a new or gone row. |
| `test_changed` | `true` when the row's `test_sha256` differs. `null` when the hashes cannot be compared. |

A comparison changes no `status` or `exit`.
`compare` is `null` without `--compare`, and when the later test did not finish; a note says why.

## A service run's summary line

The last line of a run's record, in the records folder, is its summary.
A run of an agent [inside a web service](services.md) carries three more fields in it.
They are added to version 1 and a reader that does not know them ignores them.

| Field | Meaning |
|---|---|
| `plan_digest` | HMAC-SHA256, keyed by the service token, of the plan the runner made for this run, without `elapsed_s` and `scrub`. 64 hex characters. |
| `claimed_plan_digest` | The same digest, taken by the process that claimed the run, over the plan its own run state held when it sent its last batch. `null` when that batch never came. |
| `claimed_by` | The process that claimed the run: `{"host": ..., "pid": ...}`. In a pod, `host` is the pod's name. `null` when no process claimed the run. |

The two digests are equal when the process served the plan the runner made.
When they differ, the run is an infrastructure error, with a `plan_changed` notice line in its record.
A claimed run's last batch with no digest is refused, so `claimed_plan_digest` is `null` and the run is an infrastructure error, as for a last batch that never came.
Only the digest is kept, never the plan.
Without the service token, which is never written to a record, a digest tells a reader nothing about the plan: it is not a plain hash, so a guess at a mock's value cannot be tested against it.
The service token is the one in the service's token file, which lasts across tests, so the token file must never sit on the records volume.

### The setup request's summary line

A test of a service first sends one setup request, a baseline run named `setup-` and 12 hex digits.
Its record has a summary line of the same shape as any run's, with `row` set to `baseline` and `judged` and `cost_usd` filled in.
Nothing in the line says it is a setup.
Only the `setup-` prefix of the file name and the `run` field tells it apart from a baseline run.
Scrimmage counts it in no row, no run total and no `result.json`, so a reader that groups summary lines by `test` and `row` must skip `run` names that start with `setup-`.

A call line's `call` for a process start is `process:` and the program's base name, such as `process:uname`.
A program with no plain name is `process:?`.

### A call line refused outside any request

A write made outside any request while a service run is open is refused, and the line is written into that run's record.
Its `reason` is `orphan` and its `outside` is `true`.
`background_entry` is the `background_real` entry that would let the call through, and it is absent when no entry would.
A run with such a line is an infrastructure error, because the run cannot tell the call was not its own.

### A notice line for a call outside any request

A call made outside any request while a service run is open writes a notice line into the record of each open run.
A read or a model call that is not listed in `background_real` gets the notice `lost_context`.
The run it lands in may not hold every call that belongs to it, so the run is an infrastructure error.
A call that is listed gets the notice `background`, and it runs.
Both lines carry `call`, the call's label, such as `http:stock.test` or `process:multiprocessing`.
Both also carry `background_entry`, such as `http:stock.test GET /items`.
On a `background` notice it is the `background_real` entry that lets the call through.
On a `lost_context` notice it is the entry that would let the call through.


## Values that can grow

Until the first release, a field or a value may still be renamed or removed.
A reader ignores a field it does not know.
The fields below take one of a list of values, and a release may add a value to any of them.
A reader that gets a value it does not know reads it as other.

| Field | Values today | A value it does not know |
|---|---|---|
| `command` | `test`, `check`, `record`, `init`, `show` | read as other |
| `status` | `passed`, `failed`, `valid`, `complete`, `incomplete`, `written`, `shown`, `mistake`, `infra` | read as other |
| `result` | `pass`, `fail`, `no result`, `baseline failed` | read as other |
| `outcome` | `answered`, `crashed`, `timed out`, `refused call`, `budget reached`, `not reserved`, `infrastructure error`, `stopped` | read as other |
| `judged` | `pass`, `fail`, `left out` | read as other |
| `kind` | `expect`, `forbid`, `empty`, `crashed`, `timed out`, `refused` | read as other |
| `code` | `refused`, `raised`, `status`, `differs`, `nondeterministic`, `unsavable`, `null`, `placeholder` (no longer produced, kept for documents an earlier version wrote), `too_large`, `binary`, `incomplete_reply`, `secret_changed_shape`, `model_inside`, `not_finished`, `outside_tool`, `credential`; in a suggestion, `test_mistake`, `not_agent`, `fault_not_fired`, `fault_not_changed`, `refused_call`, `refused_on_baseline`, `crash_after_fault`, `crash_on_baseline`, `retry_loop`, `timeout_after_fault`, `timeout_on_baseline`, `forbid_after_fault`, `expect_after_fault`, `empty_answer`, `rule_on_baseline`, `mock_miss_real`, `mock_miss_refused` | read as other |
| `rule` | `pattern`, `key_name` | read as other |
| `found_by` | `probe`, `static` | read as other |
| `problem` | `ambiguous`, `nested`, `not_found` | read as other |
| `reason` | `arguments`, `request`, `no_mock`, `unreadable` | read as other |
| `action` | `real`, `refused` in a mock miss; `real`, `mock`, `fault`, `refused` in a line of a run | read as other |
| `change` | `better`, `worse`, `unclear`, `no result`, `new`, `gone` | read as other |
| `stopped_by` | `budget`, `budget_held`, `no_price`, `unknown_tool_type`, `unmetered_tool`, `unseen_tool`, `structure`, `ambiguous_label`, `unknown_label`; in a test's document also `interrupted` | read as other |

`cli.v1.json` lists the values of the release that ships it, and also takes any other text there, so a reader may check a document against any version 1 copy of the schema.
A field or value it does not list then fits; the reader ignores the field and reads the value as other.
