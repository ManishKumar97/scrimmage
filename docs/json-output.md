# JSON output

`scrimmage test --json`, `scrimmage check --json` and `scrimmage record --json` print one JSON document on standard output and nothing else.
They are for a coding agent or a CI step that reads the result, not the table.
Read the document with one `json.loads` of standard output, then act on its fields and their stable values, never on message text.

The document is printed when the command ends.
A test can take minutes, so nothing is printed before then.

Its shape is the JSON Schema `scrimmage_core/schemas/cli.v1.json`, which ships in the package.
The schema is open: a field it does not list, and a value it does not list for a field that lists its values, both fit, so any version 1 copy of it reads any version 1 document.

## The envelope

Every command's document starts with the same fields:

```json
{
  "v": 1,
  "scrimmage": "0.4.0",
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
| `command` | `test`, `check`, `record` or `init`. |
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

## scrimmage test --json

```json
{
  "v": 1, "scrimmage": "0.4.0", "command": "test", "exit": 1, "status": "failed", "config": "scrimmage.toml",
  "records": "/home/acme/bot/scrimmage-runs/20261107T101500Z-a1b2c3",
  "tests": 1,
  "rows": [
    {
      "test": "order-status",
      "row": "fault 1",
      "fault": {"call": "tool:get_order", "do": "timeout", "when": "always", "model": null, "method": null, "path": null, "raises": null},
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
| `records` | The folder that holds this test's records, as the text's first line names it. `null` when the file did not load, so no folder was made. |
| `tests` | How many `[[test]]` tables the file has. |
| `rows` | One object per table row, in the table's order. Empty when the file did not load, when a signal stopped the test, or when Scrimmage itself failed. A test a refusal stopped, such as for the budget, still has every row, and the rows that did not run have `runs` 0. |
| `cost_usd` | What the model calls cost, in dollars. |
| `budget_usd` | `max_cost_usd`, or `null` when the budget is off. |
| `stopped_by` | The reason a refusal stopped the whole test, such as `budget`, or `null`. |
| `mock_misses` | The calls that had a mock on their label and matched none, one object per distinct call, below. Absent in `scrimmage record --json`. At most 200, real calls first. |
| `mock_misses_left_out` | How many more misses than the 200 listed. |

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
| `fault` | The row's fault: `call`, `do`, `when`, and `model`, `method`, `path` and `raises`, each `null` unless the fault sets it. `null` on a baseline. |
| `runs` | Runs that ran. |
| `fired` | Runs in which the fault fired. `null` on a baseline. |
| `done` | Runs that ended with an answer. |
| `passed` | Judged runs that passed. |
| `judged` | Runs that were judged. |
| `rate` | `passed` over `judged`, rounded to 4 places. `null` when no run was judged. |
| `range` | The 95% Wilson interval of the rate, as two fractions rounded to 4 places. `null` when no run was judged. |
| `pass_rate` | The share of judged runs the row needs to pass. |
| `tools` | Tool calls per run, on average. |
| `cost_usd` | What this row's model calls cost. |
| `result` | The row's result. |
| `broken` | Each rule a run broke: its `kind`, its `text` and every `runs` name that broke it, the most broken first. |
| `mock_miss_count` | The calls in this row's runs that had a mock on their label and matched none. |
| `run_details` | One object per run, below. |

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
  "v": 1, "scrimmage": "0.4.0", "command": "check", "exit": 0, "status": "valid", "config": "scrimmage.toml",
  "summary": {"tests": 2, "rows": 5, "runs": 15, "tools": 4, "budget_usd": 5.0},
  "mistakes": [], "notes": [], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `summary` | What the file holds: `tests`, `rows`, the `runs` a test would start, the `tools` listed and `budget_usd`. `null` when the file does not load. |

## scrimmage record --json

```json
{
  "v": 1, "scrimmage": "0.4.0", "command": "record", "exit": 1, "status": "incomplete", "config": "scrimmage.toml",
  "out": "scrimmage.recorded.toml",
  "records": "scrimmage-runs/20261107T101500Z-a1b2c3",
  "cost_usd": 0.0012,
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

## scrimmage init --json

```json
{
  "v": 1, "scrimmage": "0.4.0", "command": "init", "exit": 0, "status": "written", "config": "scrimmage.toml",
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

## Values that can grow

Within version 1, fields are only added: none is renamed, removed or given another type.
A reader ignores a field it does not know.
The fields below take one of a list of values, and a release may add a value to any of them.
A reader that gets a value it does not know reads it as other.
A value that is removed or renamed comes only with a new version.

| Field | Values today | A value it does not know |
|---|---|---|
| `command` | `test`, `check`, `record`, `init` | read as other |
| `status` | `passed`, `failed`, `valid`, `complete`, `incomplete`, `written`, `mistake`, `infra` | read as other |
| `result` | `pass`, `fail`, `no result` | read as other |
| `outcome` | `answered`, `crashed`, `timed out`, `refused call`, `budget reached`, `not reserved`, `infrastructure error`, `stopped` | read as other |
| `judged` | `pass`, `fail`, `left out` | read as other |
| `kind` | `expect`, `forbid`, `empty`, `crashed`, `timed out`, `refused` | read as other |
| `code` | `refused`, `raised`, `status`, `differs`, `nondeterministic`, `unsavable`, `null`, `placeholder`, `too_large`, `binary`, `incomplete_reply`, `secret_changed_shape`, `model_inside`, `not_finished`, `outside_tool`, `credential` | read as other |
| `rule` | `pattern`, `key_name` | read as other |
| `found_by` | `probe`, `static` | read as other |
| `problem` | `ambiguous`, `nested`, `not_found` | read as other |
| `reason` | `arguments`, `request`, `no_mock`, `unreadable` | read as other |
| `action` | `real`, `refused` | read as other |
| `stopped_by` | `budget`, `budget_held`, `no_price`, `unknown_tool_type`, `unseen_tool`, `structure`, `ambiguous_label`, `unknown_label` | read as other |

`cli.v1.json` lists the values of the release that ships it, and also takes any other text there, so a reader may check a document against any version 1 copy of the schema.
A field or value it does not list then fits; the reader ignores the field and reads the value as other.
