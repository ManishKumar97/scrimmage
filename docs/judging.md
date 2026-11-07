# Judging an answer

`derail test` passes or fails each row.
You write the rules. Derail compares text, with no model, so the same answer always gets the same result.

## The rules

```toml
[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]
```

`expect` is a list, and every item must match.
An item is a text, or a list of texts of which any one is enough.
So this answer needs "shipped", and either spelling of the date.

`forbid` is a list of texts the answer must not hold. Any one of them fails the run:

```toml
forbid = ["refunded"]
```

## How text is compared

The rule and the answer go through the same steps first:

- case is ignored;
- a non-breaking space, a full-width letter and other Unicode variants become the plain character;
- characters that do not show, such as a zero-width space or a soft hyphen, are dropped, so they cannot hide a word from `forbid`;
- curly quotes become straight, and the dash and non-breaking hyphen forms become `-`;
- a run of spaces or new lines becomes one space.

Then the rule must appear in the answer as plain text. It is not a word match, so:

- `"shipped"` is found in "has not shipped"; add a `forbid` for what must not be said;
- `"12"` is found in "123";
- Markdown stays, so `"Oct 30"` is not found in `Oct **30**`;
- `2²` reads as `22`, because Unicode folds a superscript to its digit.

Derail compares the answer your agent really gave. The answer in the record has secrets removed, so a rule can forbid a secret by its value and still find it.
A rule goes through the same removal before it is printed or recorded. That catches only known shapes, such as an API key or a bearer token. A secret with no known shape, such as `hunter2-password`, prints as written, so keep such a rule out of a file you share.

## Rules for a fault row

A fault can carry its own `expect` and `forbid`. Each one replaces the test's for that row:

```toml
[[test.fault]]
call   = "tool:get_order"
do     = "timeout"
expect = [["unable", "try again", "sorry"]]
forbid = ["Oct 30", "October 30"]
```

This row passes only if the bot says it could not get the order and states no date.
The test's own `expect` could not do this: a bot with no order data should not say "shipped".
Derail has no word list for "admits the failure". You write the words your agent uses.

`forbid = []` on a fault drops the test's list for that row.

A fault that fires on every call must have its own `expect` or a `forbid` with something in it.
An agent with no data can still write the expected words from general knowledge, and a rule cannot tell where the words came from, so such a row would pass when it should not. Derail refuses the file, with exit code 2.
A fault with `when = "first N"` or `"requests N"` may use the test's rules. That is right for a recovery row, where the agent should retry and give the same answer as the baseline.

If a row's rules cannot both be met, such as a fault that forbids a word the test expects, the file is refused before the first run.

## How a run counts

| How the run ended | Counts as |
|---|---|
| answered | pass or fail, by the rules |
| answered, but the answer is empty | fail: `empty answer` |
| crashed | fail: `crashed` |
| timed out | fail: `timed out` |
| refused call, on a fault row after the fault fired | fail: `refused tool:refund_order`, at any `pass_rate` |
| refused call, on a baseline | left out |
| budget reached | left out |
| infrastructure error | left out |
| stopped | left out |
| any ending, when the row's fault did not fire | left out |

A crash or a time-out is a real result about your agent, so it fails.
A refusal under a fault is the worst result there is: the tool failed, and the agent reached for a call the test does not allow, such as a refund. So its row reads `fail`, and the test still exits with code 2. Do not mock that call to make it pass: that hides what the agent did. Fix the agent.
On a baseline a refusal usually means the test forgot a mock, so it is left out.
The rest say nothing about your agent, so they are left out of every rate.
A script's answer is what [An agent run as a script](scripts.md#the-answer) says it is: the answer file, or the last line printed. Earlier lines are not read.

## How a row counts

A row passes when all its runs pass.
`pass_rate` lowers that line, on a test or on one fault. The fault's wins for its row:

```toml
pass_rate = 0.8
```

The rate is over the runs that were judged, not the runs planned.
Derail reads a row in this order:

| Result | When |
|---|---|
| `no result` | fewer than half the planned runs were judged: under the floor below |
| `fail` | passed runs divided by judged runs is below `pass_rate` |
| `no result` | a baseline run was refused |
| `pass` | anything else |

The floor is `runs // 2 + 1` judged runs, whatever `pass_rate` is: 3 of 5, 2 of 3, 1 of 1.
Under it the test did not test, so a row never passes on one run out of five.
At or above it the rate decides, and a note says how many runs counted, such as `note: order-status fault 1: 4 of 5 runs judged`.
A run where the fault did not fire does not block a row, unless so many are missing that the row falls under the floor.

With `runs = 5` and `pass_rate = 0.8`, 4 of 5 passes and 3 of 5 fails.
With `runs = 3`, 0.8 means all judged runs, as 2 of 3 is below it.

`no result` means the test did not test, so it exits with code 2, or 3 if an infrastructure error caused it.

Runs of a row whose baseline answered badly still happen. Only a baseline that crashed, timed out, was refused or reached the budget in every run skips its fault rows.

## The table

```text
test          row       fault                   runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                       5     -      5     5/5     100%  57-100%  1.0    0.00030  pass
order-status  fault 1   tool:get_order timeout  5     5      5     3/5     60%   23-88%   2.0    0.00055  fail
range: a 95% Wilson interval, from these runs alone
fail: order-status fault 1: 3/5 passed, pass_rate is 1
  2 runs: forbid "October 30" found: run-4f2a91c07b3e, run-a81c6d20f4e7
```

| Column | Means |
|---|---|
| `runs` | runs that happened in this row |
| `fired` | runs where the fault fired, `-` on a baseline |
| `done` | runs that ended "answered" |
| `passed` | passed runs of judged runs |
| `rate` | the same as a share |
| `range` | a 95% Wilson interval for that share |
| `tools` | tool and MCP tool calls per run, on average |
| `cost` | what the row's model calls cost, in dollars |

The table fits in 120 columns. A long test or fault name is cut with `...`, and the lines under the table give it in full.

The rate says what happened in these runs. It is not a promise about the next one.
The range shows how little a few runs can say: 5 of 5 reads 57-100%. The result compares the plain rate with `pass_rate`, not the range.

A line under the table names up to three runs for each rule, so you can open a run's record, `<run>.jsonl`, in the folder on the first line. For a run that crashed or timed out it gives the path of the run's log.

## Where the result is kept

Each run's summary, the last line of its record, says how that run was judged: `judged` is `pass`, `fail` or `left out`, and `broken_rules` lists what it broke.
