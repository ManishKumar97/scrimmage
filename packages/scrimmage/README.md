# scrimmage

Scrimmage tests how an AI agent behaves when its tools and its model fail.
You write the test in a `scrimmage.toml` file next to your agent.
Scrimmage runs the agent many times, injects faults such as a tool timeout or an HTTP 429 from the model, and reports how often the agent still gives a good answer.
It needs no changes to your agent's code.
It uses nothing outside Python's standard library, so installing it changes no other package.

## Install

Install it in your test environment only, never in a production image.
It runs on Python 3.11 to 3.14 and is tested on Linux.

```bash
pip install scrimmage
```

## Quickstart

Put a `scrimmage.toml` beside your agent:

```toml
[agent]
function = "acme.agent:answer"
tools    = ["acme.tools:get_order"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped"]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call   = "tool:get_order"
do     = "timeout"
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30"]
```

Then check the file, which runs nothing, and run the test:

```bash
scrimmage check
scrimmage test
```

`scrimmage init` writes a first file for you from reading your code.

## Links

- Documentation: https://manishkumar97.github.io/scrimmage/
- Source and issues: https://github.com/ManishKumar97/scrimmage
