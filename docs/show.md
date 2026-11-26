# Reading a finished test

`scrimmage test` ends with a table.
`scrimmage show` goes back into the records folder it left, and says what happened in the runs behind a failing row.
`--compare` puts two finished tests side by side, so you can see whether a change to your agent moved a pass rate.
Neither calls a model, and neither changes a file you own.

## The result file

A finished `scrimmage test` writes `result.json` into its records folder, as soon as the report is built.
It holds the document `scrimmage test --json` prints, see [JSON output](json-output.md), except that `result_file` is left out and `compare` is always `null` in the file, so a later command can read the rows without rerunning anything.
The file is readable by its owner only, as the records are.

A test that an infrastructure error or a refusal that stops the whole test, such as the budget, stopped writes no `result.json`: its rows have fewer runs than planned, and a half result is worse than none.
A test that SIGINT (Ctrl-C) or SIGTERM stopped writes one for the runs that had ended, if any had, and marks it `"stopped_by": "interrupted"` with exit 3.
`scrimmage show` reads it like any other, and its table counts only those runs.
`--compare` refuses it, as the runs are fewer than planned.
A write that fails is one warning, and the test's exit code does not change.
`--json` adds `result_file`, the file's path.

Each row in the file carries the rules that judge it, including a fault's own `expect`, `forbid` and `pass_rate`, and `test_sha256`, a hash of what the row was tested by: the question, those rules, the mocks, `run_timeout`, `real` and `tools`.
The agent's own code, prompts and model are not in the hash, because changing those is what a comparison measures.
`tools` is hashed in the order the file lists it, so a different order is a different hash.

## scrimmage show

```bash
scrimmage show [TARGET] [--run RUN] [--compare EARLIER] [--json]
```

`TARGET` is a records folder, or the `result.json` in it.
With none, it is the newest folder under `scrimmage-runs/` beside `scrimmage.toml` in the current folder.
Newest is by the time stamp in the folder's name, and only a folder that holds the result of a `scrimmage test` counts.

With no `--run`, it prints the test's header line and table, built from `result.json` by the same code `scrimmage test` uses, so the table is the one the test printed.
Then, for each failing row, the timeline of its first failing run, the rules that run broke, and the ids of the other runs of that row.

```
run-3f9a1c40b2e1  order-status  fault 1: tool:get_order timeout
outcome: answered; judged fail against the test's rules
  ms  call            gate   came back
   0  model:openai    real   tool_calls
 412  tool:get_order  fault  TimeoutError: injected (injected)
 415  model:openai    real   answer
answer: Your order shipped on Oct 30.
broken: forbid "Oct 30" found
suggestion (a heuristic): the answer holds text the test forbids, after an injected failure in tool:get_order. Scrimmage cannot see where that text came from; look at what your code gives the model when tool:get_order fails.
```

Calls are in the order they started.
A call made inside another, such as a web request inside a tool, is indented under it.
A call that had a mock on its label and matched none says `real (no mock matched)` or `refused (no mock matched)`.
`came back` is the call's error, or the start of its result, cut at 80 characters.
On a model reply a fault changed, it is what the model said, then what the agent got, read from the two replies the line keeps: `the model said Or...; garble gave the agent: Order 1234 has been สวัสดี and i...`.
The model's part is cut first, so the agent's part still shows.
When the agent got an error in place of the model's tool calls, it names the tool: `the model called get_order; truncate gave the agent: a 400 error`.
A web request sent through a client Scrimmage does not gate, such as `requests`, is a line in the gate column called `ungated`, with the library, the method and the host, and `sent around the gate` for a read or `not stopped by Scrimmage` for a write in the last column.
No gate decided it, so it is not a fault, a mock or a refusal.
At most 200 calls print for one run, and the rest are counted.
A run that crashed or timed out also prints the last line of its `.log`, cut at 200 characters, and the log's path.

`--run RUN` prints that run's timeline alone.
A run id is `run-` and 12 hex digits, and nothing else is accepted.

`show` uses the judge's words: `pass`, `fail` and `no result`.
It says a run was judged against the test's rules, never that it was wrong, because Scrimmage knows only the rules you wrote.

A folder with no `result.json`, such as one from `scrimmage record`, a test that did not finish, or one from before this file existed, shows its runs grouped by test and row, with a note that there is no table.
A run whose record has no summary line is shown as far as it was written, with a note that the run did not end.

| Code | Status | Meaning |
|---|---|---|
| 0 | `shown` | Something was shown, whatever the test's own result was. |
| 2 | `mistake` | No such folder or run, a bad run id, a folder with no run records, a record or result of a version Scrimmage does not read, or a file it refuses to open. |
| 3 | `infra` | Scrimmage itself failed. |

### Suggestions

A failing run gets at most one main suggestion, and then at most one about a mock that missed.
A suggestion is a rule over the run's record lines.
It calls no model, it writes nothing, and it never edits `scrimmage.toml` or your code.

Every suggestion is a heuristic, and its text starts `suggestion (a heuristic):`.
It has a `code` that stays the same, and `evidence`: the `seq` and `proc` of the record lines the rule read.
A program, such as a coding agent, reads `code` and not `text`, because the wording can change.
The text says "after", never "because": Scrimmage can show that one line came after another, and not what caused it.
A suggestion is not a verdict.
The verdict is the judge's, against the rules you wrote, and the suggestion only reads the record after it.

The first rule that fits is the main suggestion.
`X` is the call of the first line the gate faulted in the run.
A rule that needs `X` does not fire in a run with no such line.

| Code | Fits when | Says |
|---|---|---|
| `test_mistake` | The run was left out as `a mistake in the test` | The test has a mistake, which it names. Fix the test before reading the run. |
| `not_agent` | The run ended `budget reached`, `not reserved`, `infrastructure error` or `stopped` | The outcome says nothing about the agent. |
| `fault_not_fired` | A fault row, and the fault did not fire | The agent never called the faulted call, so the row tested nothing. |
| `fault_not_changed` | A fault row that did not fire, and a call line carries `unchanged` | The agent made the call, but the fault could not change it. The text quotes the gate's reason word for word. |
| `refused_call` | A fault row, and the agent called something the test does not allow | Look at why the agent reaches for it when the fault fires. |
| `refused_on_baseline` | A baseline, and the run ended `refused call` | Add the call to `real`, mock it, or check why the agent made it. |
| `crash_after_fault` | A fault row that fired, and the run crashed | Catch the error from `X`. |
| `crash_on_baseline` | A baseline, and the run crashed | Fix the baseline first; the last line of the log is shown. |
| `retry_loop` | A fault row that fired, the run timed out, and `X` was called 3 or more times after its fault line in that process | Cap the retries. |
| `timeout_after_fault` | A fault row that fired, the run timed out, and `X` was called fewer than 3 times after | The time went after the injected failure; handle the error from `X` so the agent answers. |
| `timeout_on_baseline` | A baseline, and the run timed out | Check `run_timeout` and the baseline. |
| `forbid_after_fault` | A fault row that fired, and a `forbid` rule broke | Look at what your code gives the model when `X` fails. |
| `expect_after_fault` | A fault row that fired, and an `expect` rule broke | Look at what the agent tells the user when `X` fails. |
| `empty_answer` | The agent gave no answer | Give a fallback answer when a step fails. |
| `rule_on_baseline` | A baseline, and an `expect` or `forbid` rule broke | The baseline fails the test's rules; fix it before reading the fault rows. |

The second kind comes after the main one, and only when the run has a line for a call that had a mock on its label and matched none:

| Code | Fits when |
|---|---|
| `mock_miss_real` | The call ran for real. Record the varying arguments with `record --runs`. |
| `mock_miss_refused` | The call is not allowed to run for real, so it was refused. Record the varying arguments with `record --runs`. |

A run that passed gets no suggestion.
`show` picks the first run judged `fail` of each failing row, so a run that was left out of the rate never appears without `--run`.
These rules therefore appear only with `--run RUN`: `test_mistake` when the judge left the run out as `a mistake in the test`, `not_agent`, `fault_not_fired`, `fault_not_changed`, and `refused_on_baseline`.
A refusal that is the test's own mistake, such as a tool no gate can reach or a hosted tool the test did not allow, reads `test_mistake` and not a refused call.
A `retry_loop` counts only calls to `X` with the same arguments as the faulted one; a loop over other arguments is `timeout_after_fault`.
Evidence lists only what a rule read: record lines by `seq` and `proc`, and `{"summary": true}` for a rule that read the run's summary line, such as `not_agent` or `rule_on_baseline`.
A run that no rule fits gets none, and `suggestions` is empty.
A fault row left out for a refusal the agent did not choose gets none either.
Only the first 200 calls of a run are read, the same ones the timeline shows, so a fault line past them leaves a rule that needs `X` silent.

### What show opens

`show` opens files read-only and opens no socket.
Before it opens a folder, a record, a log or a `result.json`, it looks at the file; it opens it without following a link, and looks again once it is open.
A symbolic link, a file owned by another user, or a file that is not a plain file is refused by name, exit 2.
A `result.json` over 16 MB is refused.
A record is read as a stream, so a very large one costs time and not memory.

### Redaction and what can still be in the output

`redact` removes known secret shapes, such as API keys and tokens, not customer data.
The records were redacted when they were written, and `show` redacts what it prints a second time.
`show`'s text and `--json` can hold customer data from the records: an answer, or the start of a tool's reply.
A `--json` document handed to a coding agent may be sent to that agent's model provider.
Read [Safety](safety.md) before you pass one on.

## Comparing two tests

```bash
scrimmage test --compare EARLIER
scrimmage show --compare EARLIER
```

`EARLIER` is a records folder or its `result.json`.
`scrimmage test --compare` runs the test, prints its output exactly as without the flag, and then prints the comparison.
`scrimmage show --compare` compares two finished tests and runs nothing.
An `EARLIER` with no `result.json`, or one of another version, is exit 2 before any run, so it costs nothing.

```
compared with scrimmage-runs/20261110T090000Z-9f8e7d (scrimmage 0.1.0rc2): 3 rows
test          row                     before          after           change
order-status  tool:check_stock error  3/3 0.44-1.00   1/3 0.06-0.79   got worse: pass -> fail; unclear: these runs cannot tell the rates apart
order-status  baseline                3/3 0.44-1.00   3/3 0.44-1.00   unclear: these runs cannot tell the rates apart
order-status  tool:get_order timeout  0/3 0.00-0.56   3/3 0.44-1.00   better (difference 0.21 to 1.00)
difference: a 95% interval of after minus before (Newcombe), from these runs alone
each row is checked at 95%, so about 1 in 20 rows that did not change reads better or worse by chance
```

### Which rows are the same row

A row's name, `fault 2`, is only its place in the file.
Add a fault above it and `fault 2` is a different fault.
So rows are matched by the test and the fault, including the fault's own `expect`, `forbid` and `pass_rate`.
Two identical faults in one test stay two rows, matched by their place among the identical ones.
A fault you edited is a new row: the old one is `gone` and the edited one is `new`.
A row whose `test_sha256` differs is still compared and is marked `test_changed`, because a change in rate may then come from the test and not from the agent.
A hash of another format cannot say whether the test changed.

### Telling two rates apart

Each side keeps its own 95% Wilson range, as the table shows it.
The difference of the two rates, after minus before, gets a 95% interval built from those two ranges and nothing else (Newcombe's method 10).

| `change` | When |
|---|---|
| `no result` | Either side has no result: too few judged runs, or `baseline failed`. This is checked first, so a thin side never gets a rate change. |
| `better` | The whole interval is above 0. |
| `worse` | The whole interval is below 0. |
| `unclear` | The interval holds 0: these runs cannot tell the two rates apart. |
| `new`, `gone` | The row is on one side only. |

Overlapping Wilson ranges do not make a change unclear: 0 of 3 against 3 of 3 overlap, and the difference is still `better`.
At 3 runs few differences clear 0, so use more runs when you want to prove a fix.
`result_change` is the judge's result on each side, such as `pass -> fail`.
A row whose result went from `pass` to `fail` is listed first, even when `change` is `unclear`.

Every row is checked at 95%, so about 1 in 20 rows that did not change reads `better` or `worse` by chance.
No correction for many rows is applied, because at these run counts it would hide real changes.
Weigh a lone change among many rows accordingly.

A comparison does not change the exit code.
The test's exit stays its own, so `pass_rate` is the one gate you set.
A script that wants "no worse" reads `compare.rows[].change`.

A `scrimmage test --compare` whose own test did not finish prints no comparison: `compare` is `null`, and a note says why.
A partial set would compare rows with fewer runs than planned.
Both commands print the comparison after their own output; with `--json` it is the `compare` field, see [JSON output](json-output.md#scrimmage-show-json).
