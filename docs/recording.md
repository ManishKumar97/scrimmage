# Recording replies

`scrimmage record` runs each test once and writes what your real services answered as mocks, so you stop writing them by hand.
It never changes your file. It writes a copy beside it, `scrimmage.recorded.toml`, and shows you the diff.

## What it does

1. Runs each test's baseline once, with the test's own mocks and nothing injected. Fault rows do not run.
2. Writes each call that ran for real as a mock with `recorded = true`.
3. Writes the copy, reads it back, and exits.

Model calls run for real and cost money, within your budget, as in `scrimmage test`. They are never recorded: a scripted model reply would replace what the test exists to see.

```bash
scrimmage record [path] [--test NAME] [--refresh LABEL] [--json]
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

A tool or MCP mock has `args`, the call's own arguments, and answers only those: see [Recorded mocks](configuration.md#recorded-mocks).

## What is never written

- A model call, embedding or moderation, or any request to a host under `model_hosts`.
- A credential, metadata or secret-store request: STS, instance metadata, Vault, an OAuth token endpoint. These are `skipped` as `credential`. In a test they run for real, as they did here.
- A Python child process as a whole. Its own tool and MCP calls are written, each from its own run of the process; its web requests have no parent in the parent's process, so they are `outside_tool`.

## Secrets

Every string in a saved value, its keys and its arguments goes through the same patterns the records use, before it leaves the agent's process.
Because a recording is meant for git, a tool's value, an MCP result and a JSON web reply also go through a key-name rule: a string under a key such as `password`, `token`, `api_key`, `secret` or `session` is replaced with `[secret removed]`. A plain text body is not parsed, so only the patterns apply there. A string that holds JSON text is read too: if a key in that text names a secret, the call is `secret_changed_shape`, as exact text cannot be cleaned.
This is a heuristic, and it is labelled as one. It is whole word parts only, so `author` and `token_count` are kept, and only strings are replaced, as a number is never a secret.
A word about the secret, as in `session_id` or `token_type`, is kept too. See `docs/limits.md` for what that costs.
Each removal is a `removed:` line and an entry in `removed`, so the reviewer sees it.
A call whose arguments held a secret, or a request whose path did, is not written at all: a mock of it could never match the real call.
A single recorded web mock answers every query and every body on its path, so a form body or an XML reply with `password=` in it is not cleaned, and the mock answers a different request the same way.
Every record prints the warning that the mocks can hold customer data. No rule can promise there is none; review them.

## Reading the output

Each mock has a comment that says where it came from: the date, the run, and the call with its arguments or method and path.
A call that could not be written is a commented skeleton at the end of its test, with the reason, so a reviewer sees the gap:

```toml
# scrimmage record could not write this mock: tool:get_invoice: returns: a bytes, which a mock cannot hold.
# Write it by hand, or leave the tool under real.
# [[test.mock]]
# call    = "tool:get_invoice"
# returns = ...
```

Being commented, it changes nothing a test reads.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every call that needs a mock got one, and every run answered |
| 1 | the file is written, and a call is listed by hand, or a run did not answer |
| 2 | a mistake: the file, a refused call, an unseen tool, the budget, or a file layout record cannot extend |
| 3 | an infrastructure error; nothing is written for a test whose record is incomplete, a bug in Scrimmage while it recorded, or Ctrl-C |

A refused call is listed `refused`, the run's calls before it are written, and a note says the calls after it were not seen. A test with a call under `skipped` or `kept` is still `complete`.

The `--json` document, with every code a call can be listed under, is in [JSON output](json-output.md).
When the file writes a test's mocks as an inline array or has a `[[test]]` line inside a multi-line string, record cannot insert safely, so it exits 2, writes nothing, and prints the mocks for you to copy in.
A diff of more than 1,000 added lines warns that it is too long to review at once: record in batches with `--test`, and rename each batch's file before the next, as the next record replaces `scrimmage.recorded.toml`.
A file of that name is replaced only if record wrote it, as its header says, and never by a result with no mocks; any other file is left alone, with exit 2.
A call listed as `instead` in the JSON needs nothing from you: the text prints it on an `instead:` line.
