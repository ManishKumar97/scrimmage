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
expect = [["unable", "sorry", "unfortunately", "cannot", "can't", "couldn't", "not able", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "i'm currently having trouble", "i'm currently having difficult", "issue retrieving", "issue with retrieving", "issue accessing", "issue with accessing", "timeout", "server error", "server issue", "trying to retrieve", "trying to look up", "trying to access", "trying to check", "i'm experiencing", "i'm still experiencing", "i am experiencing", "i'm currently experiencing", "technical difficult"]]
forbid = ["shipped", "Oct 30", "October 30", "Oct. 30", "10/30"]
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
