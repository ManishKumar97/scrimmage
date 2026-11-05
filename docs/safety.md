# Safety

A test runs your agent many times.
A tool that changes data, such as `refund_order`, must never run for real by accident.

Nothing can reliably tell a read from a write by looking at a tool, so Faultline does not try.

## Every tool is refused unless the test allows it

A tool call runs for real only if it is listed under `[agent] real`.
Any tool can be mocked instead, write tools included, so a test can give `refund_order` a fixed reply and see what the agent does next.

| Tool call, not mocked | Listed under `real` | What happens |
|---|---|---|
| `tool:check_inventory` | yes | Runs for real |
| `tool:refund_order` | no | Refused; the run stops as "refused call" |

A refused call raises `FaultlineRefused`, an error of Faultline's own.
Its message names the call and the two fixes: mock it, or add it to `real` if it is safe.

Your code, or a framework, might swallow that error and carry on, and the next tool could be another write.
So one refusal ends the run: Faultline fails every gated call after it, and the runner stops the program.

If you have decided every tool is safe, write `real = ["tool:*"]`; the run table then says so in its header.

## What a model request may do

Model calls run for real, because they are what is being tested.
Everything around them is checked:

- A hosted MCP server can change data at the provider, where no gate can see it. A request that offers one is never sent unless you list it as `hosted:mcp/<label>` under `real`.
- A tool of a type Faultline does not know stops the test, so a new provider feature costs a table update, never a safety gap.
- A tool the model is offered that no gate sees could run for real unchecked, so the request is not sent and the test stops.
- A request body Faultline cannot read is refused, because the tools in it cannot be checked.
- Any other write to a model host, such as starting a fine-tuning job, is refused.

## A gap in this version

A write to a host that is not a model host is **not** refused yet, even when it goes through httpx2. Only your listed tools guard it: a tool under `tools` that is neither mocked nor under `real` never runs, so the write inside it never happens. A write sent from code outside those tools passes. Web-request refusal comes in a later version.

## The budget

A test has a budget, $5 unless you change it. When the runner cannot be reached to check it, the call is refused rather than sent.
See [Failing the model, and keeping a budget](model-faults.md).

## A misspelled tool never runs ungated

If `tools` names a function or a module that does not exist, the run stops as a mistake in the test.
Otherwise a typo such as `refund_ordr` would leave the real `refund_order` running with no gate.

## Staying out of production

- Installing or importing Faultline never switches anything on.
- The startup file is never on Python's default path; only `faultline-run` puts it there.
- The SDK switches on only when a test run gives it its secret, and then prints that it is active.

In production you start your agent the usual way, and Faultline is never loaded.

## faultline.toml runs code

`function` names code that runs, so treat `faultline.toml` like code.
A CI job that holds model API keys must not run `faultline test` on a pull request from an outside contributor.

## Records

Records stay on your machine, are readable only by you, and have known secret patterns removed. Headers are never recorded, so your API key does not reach a record.
The per-run `.log` file holds whatever your agent printed and is not cleaned, so keep it like any test log.
