# Safety

A test runs your agent many times.
A tool that changes data, such as `refund_order`, must never run for real by accident.

Nothing can reliably tell a read from a write by looking at a tool, so Derail does not try.

## Every tool is refused unless the test allows it

A tool call runs for real only if it is listed under `[agent] real`, in your program or in a child process that joined the run.
Any tool can be mocked instead, write tools included, so a test can give `refund_order` a fixed reply and see what the agent does next.

| Tool call, not mocked | Listed under `real` | What happens |
|---|---|---|
| `tool:check_inventory` | yes | Runs for real |
| `tool:refund_order` | no | Refused; the run stops as "refused call" |

A refused call raises `DerailRefused`, an error of Derail's own.
Its message names the call and the two fixes: mock it, or add it to `real` if it is safe.

Your code, or a framework, might swallow that error and carry on, and the next tool could be another write.
So one refusal ends the run: Derail fails every gated call after it, and the runner stops the program.

If you have decided every tool is safe, write `real = ["tool:*"]`; the run table then says so in its header.

## What a model request may do

Model calls run for real, because they are what is being tested.
Everything around them is checked:

- A hosted MCP server can change data at the provider, where no gate can see it. A request that offers one is never sent unless you list it as `hosted:mcp/<label>` under `real`.
- A tool of a type Derail does not know stops the test, so a new provider feature costs a table update, never a safety gap.
- A tool the model is offered that no gate sees could run for real unchecked, so the request is not sent and the test stops.
- A chat request body Derail cannot read is refused, because the tools in it cannot be checked.
- When a fault names a model, a call its label names with no model name Derail can read is refused with `unreadable_model`, because the fault could hit the wrong call. See [Failing one model](model-faults.md#a-request-with-no-name-derail-can-read).
- Any other write to a model host, such as starting a fine-tuning job, is a web request, so it is refused unless `real` lists it.
- A model call redirected to a host that is not a model host is refused, so it cannot leave the budget behind. If your gateway really does redirect to another host, name that host under `model_hosts`.

## What a web request may do

A web request through httpx or httpx2 is decided by its method, its path and the host in its own URL.

| Web request, not mocked | Listed under `real` | What happens |
|---|---|---|
| `GET` or `HEAD` | either way | Runs for real |
| `POST`, `PUT`, `PATCH`, `DELETE` or any other method | yes | Runs for real |
| `POST`, `PUT`, `PATCH`, `DELETE` or any other method | no | Refused; the run stops as "refused call" |

Each hop of a redirect is decided on its own, so a redirect cannot turn a read into a write that goes unchecked.
A refusal here ends the run, as it does for a tool.

A `GET` that changes data, which some services allow, is not caught. Mock it.
Other HTTP libraries are not gated. Only your listed tools guard a write sent with one: a tool under `tools` that is neither mocked nor under `real` never runs, so the write inside it never happens.

## What an MCP tool may do

An MCP tool call is refused unless a test mocks it or `real` lists it, like a Python tool.

| MCP tool call, not mocked | Listed under `real` | What happens |
|---|---|---|
| `mcp:docs/search` | yes | Runs for real |
| `mcp:docs/delete_doc` | no | Refused; the run stops as "refused call" |

A tool the server marks as read-only is refused all the same, because that mark is only a hint from the server.
The refusal says so.
A refusal happens at the MCP gate, so over streamable HTTP the server never sees the call.
Requests that are not tool calls, such as `tools/list`, pass; a request Derail does not know is refused. See [MCP tools](mcp.md).

Some of the requests that pass can still do harm.
`resources/read`, `prompts/get`, `completion/complete` and `resources/subscribe` run code on the server, and a server can do anything in that code, such as count a read or send an email.
Derail does not stop them, because nothing marks which ones are safe.
Each one leaves a line in the record with the action `real`, so you can see that it happened.
If a stray one would do real harm, point the test at a server that cannot do it.

If a label such as `mcp:search` could mean a tool on two servers, the test stops at the first call after both servers are known, so from then on a read on one server cannot be taken for a write on another.
A call made before the second server was listed or called goes by that label. See [Limits](limits.md).

## Child processes

The rules above reach every process that joined the run, so a child's `refund_order` is refused like your program's, and the refusal ends the run.
A forked child's refusal reaches the runner before the error reaches your code, so it is not lost when the child ends with `os._exit`.
A child that cannot get its plan refuses each call, and the run is an infrastructure error.

A Python child that would not join, because it has its own environment or a flag such as `-I`, is refused where it starts, unless `real` lists `process:python`.
Only a start made from Python is seen, so `sh -c 'python x'` is not. See [Limits](limits.md).

So a rerun never reaches a write through a child: the child joins and is refused, or never starts.
The exceptions are all in [Limits](limits.md).

A program on another Python is not gated, and it may be something unrelated, such as `pip`, so it runs untouched.
But it cannot import a module that holds a tool under `tools`, or run that file: the import fails or the program stops, and the run ends as "refused call".
A web request or an MCP call from a program on another Python is not stopped. See [Limits](limits.md).

## The budget

A test has a budget, $5 unless you change it. When the runner cannot be reached to check it, the call is refused rather than sent.
See [Failing the model, and keeping a budget](model-faults.md).

## A misspelled tool never runs ungated

The rule covers every tool a gate reaches: the ones listed under `tools`, and LangChain tools, which need no list.

If `tools` names a function or a module that does not exist, the run stops as a mistake in the test.
Otherwise a typo such as `refund_ordr` would leave the real `refund_order` running with no gate.

## What the gates assume

Derail stops ordinary code from running a write by accident.
It runs inside your program, so it cannot stop code written to get around it.
Code that deliberately works around a gate, or calls the plain function directly, is outside what it covers.
So a test is not a sandbox.
If one stray call to a tool would do real harm, make that tool unreachable from the test environment, with test credentials or a network that cannot reach the real system.

## Staying out of production

- Installing or importing Derail never switches anything on.
- The startup file is never on Python's default path; only `derail-run` puts it there.
- The SDK switches on only when a test run gives it its secret, and then prints that it is active. A child process that joins prints nothing, and the record lists it.

In production you start your agent the usual way, and Derail is never loaded.

## derail.toml runs code

`function` names code that runs, so treat `derail.toml` like code.
A CI job that holds model API keys must not run `derail test` on a pull request from an outside contributor.

## Records

Records stay on your machine, are readable only by you, and have known secret patterns removed. Headers are never recorded, so your API key does not reach a record.
A process's command line is recorded with those patterns removed, so a secret in an unknown format stays in it. Pass secrets in the environment, which is never recorded.
The per-run `.log` and `.out` files hold whatever your agent printed and are not cleaned, so keep them like any test log.
