# JSON output

`derail test --json` and `derail check --json` print one JSON document on standard output and nothing else.
They are for a coding agent or a CI step that reads the result, not the table.
Read the document with one `json.loads` of standard output, then act on its fields and their stable values, never on message text.

The document is printed when the command ends.
A test can take minutes, so nothing is printed before then.

Its shape is the JSON Schema `derail_sdk/schemas/cli.v1.json`, which ships in the package.
The schema is open: a field it does not list, and a value it does not list for a field that lists its values, both fit, so any version 1 copy of it reads any version 1 document.

## The envelope

Every command's document starts with the same fields:

```json
{
  "v": 1,
  "derail": "0.4.0",
  "command": "test",
  "exit": 1,
  "status": "failed",
  "config": "derail.toml",
  "mistakes": [{"message": "order-status fault 1: the fault never fired in any run, so the row tested nothing"}],
  "notes": [],
  "warnings": [],
  "infra": []
}
```

| Field | Meaning |
|---|---|
| `v` | The version of this format, 1. |
| `derail` | The installed Derail version, so a document can be traced to the release that made it. `unknown` when Derail runs with no package metadata, such as from a source tree. |
| `command` | `test` or `check`. |
| `exit` | The process's exit code. Always equal to it. |
| `status` | A word for the exit code, per command; see below. |
| `config` | The file read, as given on the command line. |
| `mistakes` | One item per line the text prints after `test mistake:`, or per problem in the file. |
| `notes` | One item per line the text prints after `note:`. |
| `warnings` | One item per line the text prints after `warning:`. With `real = ["tool:*"]` the first one says that every tool that is not mocked runs for real; the text prints it above the table. |
| `infra` | One item per line the text prints after `infrastructure error:`, and the reason Derail itself stopped, such as `stopped by SIGTERM`. When a run failed by itself, it points at the runs whose `outcome` is `infrastructure error`, in `run_details`. |

Each item is an object with one field, `message`, the line's text: `{"message": "..."}`.
It is an object so a later version can add a field to it, such as a code, without changing its type.
The text of a message can change in any release.

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `test` | `passed` | `failed` | `mistake` | `infra` |
| `check` | `valid` | | `mistake` | `infra` |

## derail test --json

```json
{
  "v": 1, "derail": "0.4.0", "command": "test", "exit": 1, "status": "failed", "config": "derail.toml",
  "records": "/home/acme/bot/derail-runs/20261107T101500Z-a1b2c3",
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
| `rows` | One object per table row, in the table's order. Empty when the file did not load, when a signal stopped the test, or when Derail itself failed. A test a refusal stopped, such as for the budget, still has every row, and the rows that did not run have `runs` 0. |
| `cost_usd` | What the model calls cost, in dollars. |
| `budget_usd` | `max_cost_usd`, or `null` when the budget is off. |
| `stopped_by` | The reason a refusal stopped the whole test, such as `budget`, or `null`. |

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

A file that does not load gives the envelope with `rows: []`, `records: null` and each problem in `mistakes`, with exit 2.
With `--json`, once Derail has started, Ctrl-C or SIGTERM while Derail loads, while the file is read or while runs go stops the command with `status` `infra`, exit 3, `rows: []` and the reason in `infra`, such as `stopped by SIGTERM`.
Runs that were going are stopped first. `records` names the records folder if the test had made one.
A failure of Derail itself gives the same, with the error in `infra`.
Derail has started from the moment its entry point runs, a few tens of milliseconds after Python does; a signal before that ends the process with no document.
Once the result is settled, a signal is ignored until the process exits, so its exit code always equals the document's `exit`.
SIGKILL cannot be caught, so it leaves no document.

## derail check --json

```json
{
  "v": 1, "derail": "0.4.0", "command": "check", "exit": 0, "status": "valid", "config": "derail.toml",
  "summary": {"tests": 2, "rows": 5, "runs": 15, "tools": 4, "budget_usd": 5.0},
  "mistakes": [], "notes": [], "warnings": [], "infra": []
}
```

| Field | Meaning |
|---|---|
| `summary` | What the file holds: `tests`, `rows`, the `runs` a test would start, the `tools` listed and `budget_usd`. `null` when the file does not load. |

## Values that can grow

Within version 1, fields are only added: none is renamed, removed or given another type.
A reader ignores a field it does not know.
The fields below take one of a list of values, and a release may add a value to any of them.
A reader that gets a value it does not know reads it as other.
A value that is removed or renamed comes only with a new version.

| Field | Values today | A value it does not know |
|---|---|---|
| `command` | `test`, `check` | read as other |
| `status` | `passed`, `failed`, `valid`, `mistake`, `infra` | read as other |
| `result` | `pass`, `fail`, `no result` | read as other |
| `outcome` | `answered`, `crashed`, `timed out`, `refused call`, `budget reached`, `not reserved`, `infrastructure error`, `stopped` | read as other |
| `judged` | `pass`, `fail`, `left out` | read as other |
| `kind` | `expect`, `forbid`, `empty`, `crashed`, `timed out`, `refused` | read as other |
| `stopped_by` | `budget`, `budget_held`, `no_price`, `unknown_tool_type`, `unseen_tool`, `structure`, `ambiguous_label`, `unknown_label` | read as other |

`cli.v1.json` lists the values of the release that ships it, and also takes any other text there, so a reader may check a document against any version 1 copy of the schema.
A field or value it does not list then fits; the reader ignores the field and reads the value as other.
