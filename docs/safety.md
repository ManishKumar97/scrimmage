# Safety

A test runs your agent many times.
A tool that changes data, such as `refund_order`, must never run for real by accident.

Which tools change data cannot be detected reliably, so Faultline does not guess.

## Every tool is refused unless the test allows it

A tool call runs for real only if it is listed under `[agent] real`.
Any tool can be mocked instead, write tools included, so a test can give `refund_order` a fixed reply and see what the agent does next.

| Tool call, not mocked | Listed under `real` | What happens |
|---|---|---|
| `tool:check_inventory` | yes | Runs for real |
| `tool:refund_order` | no | Refused; the run stops as "refused call" |

A refused call raises `FaultlineRefused`, an error of Faultline's own.
Its message names the call and the two fixes: mock it, or add it to `real` if it is safe.

A framework or your own code might catch that error and let the model try another tool, which could be another write.
So the first refusal marks the whole run as refused: every later gated call fails at once, and the runner stops the program.

If you have decided every tool is safe, write `real = ["tool:*"]`; the run table then says so in its header.

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

Records stay on your machine, are readable only by you, and have known secret patterns removed.
The per-run `.log` file holds whatever your agent printed and is not cleaned, so keep it like any test log.
