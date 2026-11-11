# Recording replies

`scrimmage record` runs each test's baseline, once or several times, and writes what your real services answered as mocks, so you stop writing them by hand.
It never changes your file. It writes a copy beside it, `scrimmage.recorded.toml`, and shows you the diff.

## What it does

1. Runs each test's baseline once, or `--runs N` times, with the test's own mocks and nothing injected. Fault rows do not run.
2. Writes each call that ran for real as a mock with `recorded = true`.
3. Writes the copy, reads it back, and exits.

Model calls run for real and cost money, within your budget, as in `scrimmage test`. They are never recorded: a scripted model reply would replace what the test exists to see.

```bash
scrimmage record [path] [--test NAME] [--refresh LABEL] [--runs N] [--json]
```

`--test NAME` records only that test, and can be given more than once. A name that is not in the file exits 2 before any run.

`--refresh LABEL` records a label again that already has a mock, such as after an API changed. The mocks on that label leave the plan and the output, so the call runs for real. It stops with exit 2 before any run for a label that would not run for real: a tool or an MCP tool not under `real`, or a web label whose mocks are not all on reads or under `real`.

```
$ scrimmage record
scrimmage record: 2 tests, 2 runs; records in scrimmage-runs/20261107T101500Z-a1b2c3
order-status  run-3f9a1c40b2e1  answered  wrote 2, kept 1, skipped 1, by hand 1
stock-level   run-8e21d07a9c33  answered  wrote 1
wrote 3 mocks to scrimmage.recorded.toml; scrimmage.toml is unchanged
recorded 3 of 4 calls that needed a mock
by hand: order-status: tool:get_invoice: returns: a bytes, which a mock cannot hold
skipped: order-status: http:sts.amazonaws.com POST /: a credential request is never recorded; it runs for real, as in the recording
removed: order-status: tool:get_user: a value under 'access_token' (heuristic: by its key's name)
warning: the mocks hold replies from real systems, which can hold customer data; check them before you commit them
model calls cost $0.0012 of a budget of $5
next: diff scrimmage.toml scrimmage.recorded.toml, then scrimmage test scrimmage.recorded.toml
```

## Recording more than one run

A model does not always send the same arguments.
One run sees one form, and a later `scrimmage test` can send another, which misses the mock.
`--runs N` runs each chosen test's baseline N times and writes one exact mock for each set of arguments the runs sent.
It adds no matcher: `args` stays exact and type-strict, so `1`, `1.0`, `true` and `"1"` are four sets.

```
$ scrimmage record --runs 5
scrimmage record: 5 runs of each of 1 test; each calls these for real: tool:check_inventory, tool:get_stock
scrimmage record: 1 test, 5 runs each, 5 runs; records in scrimmage-runs/20261111T090000Z-1a2b3c
order-status  5 of 5 answered  wrote 4, kept 2
  run 1  run-28e4d3bcb0a2  answered  2 of 2 calls written
  run 2  run-71c0aa9e4b10  answered  2 of 2 calls written
arguments: order-status: tool:get_stock: 2 sets in 5 runs: {"sku": "SKU-42"} in 4, {"sku": "42"} in 1; each has its own mock
model calls cost $0.0008 of a budget of $0.01, 5 runs
```

- **A run** is one baseline run of one test, as in `scrimmage test`. Three tests and `--runs 5` are 15 runs.
- **The default is 1**, which is today's command, cost and output. The file's `runs` is for `scrimmage test` and is not read, so an unchanged `scrimmage record` never costs more.
- **`N` is a whole number from 1 to 1000.** Anything else exits 2 before any run. The runner builds every row before it checks the budget, so the budget is not a cap on N.
- **The budget covers every run of every test.** `max_cost_usd` is one cap for the whole command.
- **Tools under `real` run N times.** The command names every label under `real` before the first run. With N above 1, no `max_cost_usd` and a label under `real`, it exits 2 before any run: N times an unbounded spend and N times every real call is too much to start by a flag alone. Set `max_cost_usd`, or record with `--runs 1`.
- **Runs follow `[agent] parallel`**, as `scrimmage test` does, so they can overlap on the services under `real`. The file written does not depend on which run finished first: mocks are in the order of the runs as submitted.

When the runs disagree, a mock answers only its own key and holds a reply some run got for exactly that key.

| Case | What is written | Exit |
|---|---|---|
| Different arguments for one tool | One exact mock for each set | 0 |
| A different tool order | The mocks of every call any run made. Order is not recorded | 0 |
| A call seen in only some runs | Its mock. The comment and `--json` say in how many runs | 0 |
| The same key, unequal replies | No mock. Listed `nondeterministic`, with a skeleton | 1 |
| The same tool arguments succeeded in one run and failed in another | No mock. Listed `nondeterministic` | 1 |
| A tool raised in one run and succeeded in another | No mock of the tool, and none of its web requests from any run | 1 |
| A web request saved in one run and listed in another (a 503, a binary reply) | No mock from any run, the run that listed a call too. Listed `nondeterministic` | 1 |
| A model call or credential request beneath a tool in one run only | No mock of that tool from any run. Its web requests follow their own rules | 1 if listed |
| A refusal in one run | The refused call is listed. Every run's calls are written, the refused run's calls before the refusal too. The runs not yet started never start | 2 |
| A run crashed or timed out | Its calls are written | 1 |
| A run is an infrastructure error, or a Scrimmage bug was found | Nothing for the whole test | 3 |

The cross-run web rule leaves out calls skipped as `outside_tool` or `credential`: those are not failures of the same request.
A tool call listed with a code gets one skeleton for each set of arguments, so two sets listed with one code are two skeletons, each with its own `args` line.
A call with clean arguments on a key that another run found a model call, a credential request or an unwritable call beneath is listed too, and its message names the runs it was made in.

A test that a stop of the whole command cut short, with fewer runs than asked, writes nothing, and the text and `--json` say how many runs did not start.
A stop on the budget writes nothing at all, and an earlier `scrimmage.recorded.toml` is kept byte for byte: some tests would look whole, and the old file's mocks would be lost to a half-done record.
It exits 2, or 3 with an infrastructure error. Raise `max_cost_usd`, lower `--runs`, or record in batches with `--test`.
This applies at N = 1 too: a budget stop used to write the calls made before the refusal.

How many runs are enough depends on how often the model sends the rarer form.
If it sends it in a fraction p of runs, here p = 0.25 as an example, the chance that N runs never see it is (1 - p) to the N:

| N | Both forms seen | The rarer form never seen |
|---|---|---|
| 1 | 0 | 0.75 |
| 3 | 0.56 | 0.42 |
| 5 | 0.76 | 0.24 |
| 10 | 0.94 | 0.06 |
| 20 | 0.997 | 0.003 |

So `--runs` makes a miss rarer. It cannot make one impossible: a form never seen while recording still misses, as [When a replay misses a mock](#when-a-replay-misses-a-mock) says.

### A reply that changes with time

Matching a reply is strict equality, and recording keeps it so: one reply is never frozen as if it were the only one.
A tool whose reply holds the time changes in every run, such as `kubectl get pods` with its AGE column, a Prometheus query with sample timestamps, or a log tail.
At N above 1 such a key is `nondeterministic` and gets no mock, where N = 1 would have written one. So more runs can write fewer mocks.
The listing names the runs whose replies differed.
For such a tool, record it with `--runs 1`, write its mock by hand, or leave it under `real`.

## What gets written

A call is written only if it ran for real at a gate. Mocks you wrote stay in the plan and win, so a write tool's hand-written mock answers and it never runs; such a call is `kept`.

| The real tool call | Written |
|---|---|
| made no gated call | a mock of the tool |
| made gated calls, each writable, already mocked, or a credential request | those calls' mocks, so the tool's own code still runs in a test; a credential request runs for real |
| made a gated call that cannot be written, such as a PNG reply, a redirect or a reply over the cap | a mock of the tool, if its value can be and nothing below it is forbidden; otherwise the tool is listed |
| got a reply a mock must not copy (a 401, 403, 407, 408, 429 or 5xx, a redirect excepted), a request failed or was refused, a reply held a secret that could not be taken out, or a credential request sits beside a call that cannot be written | no mock of that call and no mock of the tool, which would freeze the failure into every run or copy the secret; its other calls are written |
| made a model call | its other calls' mocks, and never a mock of the tool, which would freeze a model reply |

A web request is written only when it was made inside a real tool call a gate linked. A request your agent makes itself, or one a tool makes from a thread, is `skipped` as `outside_tool`; it runs for real in a test as it did here, a GET for real, a write only if it is under `real`.

A web reply is written as `json` when TOML can hold it, or as exact `text` with its `content-type` when it holds a `null` or an integer beyond 64 bits. No other header is written.

Only replies that are data are written: 2xx, and 4xx other than 401, 403, 407, 408 and 429. A 5xx written as a mock would be a fault nobody injected in every run.

A tool or MCP mock has `args`, the call's own arguments, and answers only those: see [Mocks that match arguments](configuration.md#mocks-that-match-arguments).
An argument that differs on a later run is a miss, which the report shows; see [When a replay misses a mock](#when-a-replay-misses-a-mock).

## What is never written

- A model call, embedding or moderation, or any request to a host under `model_hosts`.
- A credential, metadata or secret-store request: STS, instance metadata, Vault, an OAuth token endpoint. These are `skipped` as `credential`. In a test they run for real, as they did here.
- A Python child process as a whole. Its own tool and MCP calls are written, each from its own run of the process; its web requests have no parent in the parent's process, so they are `outside_tool`.

## Secrets

Every string in a saved value, its keys and its arguments goes through the same patterns the records use, before it leaves the agent's process.
Because a recording is meant for git, a tool's value, an MCP result and a JSON web reply also go through a key-name rule: a string under a key such as `password`, `token`, `api_key`, `secret` or `session` is replaced with `[secret removed]`. A plain text body is not parsed, so only the patterns apply there. A string that is JSON text, as a whole, is read too: if a key in that text names a secret, the call is `secret_changed_shape`, as exact text cannot be cleaned. Text around the JSON, as in `Result: {...}`, and doubly encoded JSON are not read.
This is a heuristic, and it is labelled as one. It is whole word parts only, so `author` and `token_count` are kept, and only strings are replaced, as a number is never a secret.
A word about the secret, as in `session_id` or `token_type`, is kept too. See `docs/limits.md` for what that costs.
Each removal is a `removed:` line and an entry in `removed`, so the reviewer sees it.
A value can be listed twice: a Basic credential under a header key such as `Authorization` is removed by the pattern and again by the key-name rule, so it has a `pattern` and a `key_name` entry.
A call whose arguments held a secret, or a request whose path did, is not written at all: a mock of it could never match the real call.
A `password=` or `token=` field in a reply body is removed, as it is anywhere in a recorded string, and so is the value after `Authorization: Basic`. An XML element value, as in `<password>hunter2</password>`, is not cleaned, as no pattern reads it. A request body is not written into the mock, and a single recorded web mock answers every query and every body on its path, so a mock answers a different request the same way.
Every record prints the warning that the mocks can hold customer data. No rule can promise there is none; review them.

## Reading the output

Each mock has a comment that says where it came from: the date, the run, and the call with its arguments or method and path.
A call that could not be written is a commented skeleton at the end of its test, with the reason, so a reviewer sees the gap:

```toml
# scrimmage record could not write this mock: tool:get_invoice: returns: a bytes, which a mock cannot hold.
# Write it by hand, or leave the tool under real.
# [[test.mock]]
# call    = "tool:get_invoice"
# args    = {invoice_id = "INV-7"}
# returns = ...
```

Being commented, it changes nothing a test reads.
The `args` line is written only when the call's arguments were read whole, held no secret and were plain, so it can be uncommented as it is.
When a tool under `real` by name now has a mock with `args`, `record` adds a note: a call with other arguments runs for real there.

## When a replay misses a mock

A recording keeps the arguments the model sent that once.
If a later run sends another form, such as `"SKU-42"` where the recording has `"42"`, no mock matches.
The call runs for real if its label is under `real` by name, and is refused if it is not.
The report shows the arguments sent and the nearest mock.

You can:

1. Fix the tool description or the prompt, so the model sends one form.
2. Record more runs with `scrimmage record --runs N`, on your own file, so every form the model sends in those runs gets its own mock. Or add a second mock for the other value by hand.
3. Remove `args`, so one reply answers any value.
4. Remove the tool from `real`, so a miss stops the run.

Recording again on the recorded file has limits.
It writes to `<name>.recorded.recorded.toml`, as `record` writes `<stem>.recorded.toml`.
It captures the other value only if the model sends it in that record's runs.
It works only for a label under `real` by name: under `tool:*`, and for a label not listed, a miss is refused and nothing is written for that call.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every call that needs a mock got one, and every run answered |
| 1 | the file is written, and a call is listed by hand, or a run did not answer |
| 2 | a mistake: the file, `--runs`, a refused call, an unseen tool, the budget (which writes nothing), or a file layout record cannot extend |
| 3 | an infrastructure error; nothing is written for a test whose record is incomplete, a bug in Scrimmage while it recorded, or Ctrl-C |

A refused call is listed `refused`, the run's calls before it are written, and a note says the calls after it were not seen. A test with a call under `skipped` or `kept` is still `complete`.

The `--json` document, with every code a call can be listed under, is in [JSON output](json-output.md).
When the file writes a test's mocks as an inline array or has a `[[test]]` line inside a multi-line string, record cannot insert safely, so it exits 2, writes nothing, and prints the mocks for you to copy in.
A diff of more than 1,000 added lines warns that it is too long to review at once: record in batches with `--test`, and rename each batch's file before the next, as the next record replaces `scrimmage.recorded.toml`.
A file of that name is replaced only if record wrote it, as its header says, and never by a result with no mocks; any other file is left alone, with exit 2.
A call listed as `instead` in the JSON needs nothing from you: the text prints it on an `instead:` line.
