# Security

This page says how Scrimmage protects your systems and your data while it tests an agent, and where that protection ends.
Each statement here is enforced by code in the `scrimmage` package and checked by its test suite.
For the rules a test follows call by call, see [Safety](safety.md).

## Your data stays on your machine

- Scrimmage has no telemetry and no hosted service.
  It sends nothing to its authors or to any third party.
- The runner and the code inside your agent talk over one local connection: a control server that listens on `127.0.0.1` only, on a port chosen for each test.
- The `scrimmage` package uses only Python's standard library, so it brings in no network client of another project.
- Records are written to `scrimmage-runs/` next to your `scrimmage.toml`.
  Scrimmage writes a `.gitignore` there, so the records are not committed by accident.

Your agent's own calls, such as model calls to your provider, go where your agent sends them, as they would without Scrimmage.

## Writes are refused unless the test allows them

A test runs your agent many times, so a call that changes data must never run for real by accident.
Nothing can reliably tell a read from a write by looking at a call, so Scrimmage does not guess.

- A listed tool or an MCP tool runs for real only if the test lists it under `[agent] real`.
  Otherwise it is mocked or refused.
- A web request through httpx or httpx2 with a method other than `GET` or `HEAD` is refused unless `real` lists it.
- A refused call raises `ScrimmageRefused`, an error type of Scrimmage's own.
  Code that catches a library's errors, such as `OSError` or `TimeoutError`, does not catch it.
- One refusal ends the run.
  Every gated call after it is refused too, so code that swallows the error cannot go on to the next write.
- A hosted MCP server acts at the model provider, where no gate can see it.
  A model request that offers one is never sent unless `real` lists it.
- A model call that is redirected to a host that is not a model host is refused.
- A Python child process that would run without the gates, because it has its own environment or a flag such as `-I`, is refused when your Python code starts it.

See [Safety](safety.md) for each rule and its exceptions.

## The plan is data, and imports are limited

For each run, the runner sends the code inside your agent a plan: which calls to mock, which to fail, and which may run for real.
The plan is data only.
It holds no code.

Two settings name Python objects: a mock's `type` and a fault's `raises`.
Scrimmage imports such a name only if it is under one of your agent's own top-level packages, the ones that `function` and `tools` name.
The check runs before the import, and at the start of each process, so a plan that names anything else stops the program before your agent runs.

## The control channel is signed

The runner makes a new random token for each `scrimmage test`, 256 bits long.
It passes the token to the agent's processes in the `SCRIMMAGE_TOKEN` environment variable.

- The token itself is never sent.
  Each request is signed with HMAC-SHA256 over its method, path, time, a one-time nonce and a hash of its body.
- A request whose time is more than 60 seconds from the server's clock is refused.
- A nonce used before is refused, so a request cannot be replayed.
- Each reply is signed over the request's own signature, its status and a hash of its body, so a reply cannot be moved to another request.

Any process in the run can read `SCRIMMAGE_TOKEN`, because a child process needs it to join the run.
So the signing protects the channel from other programs on the machine, not from code inside the agent under test.

## Secrets in records

Every call is written to a record, so Scrimmage keeps known secrets out of it.

- The record folder is readable only by you: folders are created with mode `0700` and files with mode `0600`.
- A web request's line holds its method and its path.
  Its headers, its cookies and its query string are never recorded.
- The environment is never recorded, and neither is the run's token.
- Text that is recorded, such as tool arguments and results, model requests and replies, error messages, answers and command lines, has known secret shapes replaced with `[secret removed]`:
    - OpenAI and Anthropic API keys, such as `sk-...` and `sk-proj-...`;
    - AWS access key ids, `AKIA...`;
    - Google API keys, `AIza...`;
    - GitHub tokens, `ghp_...` and the other `gh?_` forms;
    - Slack tokens, `xoxb-...` and the like;
    - bearer tokens, `Bearer ...`;
    - JSON Web Tokens, `eyJ...`.
- In any URL that is recorded, the query, the fragment and a user name or password are replaced with `[removed]`.

These are pattern matches, a heuristic: they remove secrets of a known shape only.
See the limits below.

## Real spending has a cap

Model calls run for real, so a test has a budget, $5 unless you change `max_cost_usd`.

- Each model call reserves an estimate of its cost before it is sent.
  A call that does not fit in what is left is not sent.
- If the runner cannot be reached to check the budget, the call is refused rather than sent.

See [Failing the model, and keeping a budget](model-faults.md).

## Staying out of production

- Installing or importing Scrimmage switches nothing on.
- Its startup file is never on Python's default path.
  Only `scrimmage-run`, which `scrimmage test` uses for every run, puts it there.
- The code inside your agent switches on only when a test run gives it its token.
  If it then cannot set up every gate, the program stops before your agent runs, rather than running without them.

## What Scrimmage does not protect against

- **Code written to get around it.** The gates run inside your program.
  They stop accidents, not code that sets out to evade them, such as calling a tool's plain function directly or patching a library.
  A test is not a sandbox.
  If one stray call would do real harm, make that system unreachable from the test environment, with test credentials or a network that cannot reach it.
- **Calls Scrimmage does not see.** Only httpx and httpx2 are gated.
  A write sent with another HTTP library, or a raw socket, is not refused or recorded.
  A program on another Python, or a child started through a shell, is not gated either.
- **Secrets of an unknown shape.** Redaction removes known shapes only.
  A plain `password=hunter2` in a tool's result or a command line is recorded as written.
  Pass secrets in the environment, which is never recorded.
- **Your agent's own output.** The per-run `.log` and `.out` files hold whatever your agent printed, and they are not cleaned.
  They are readable only by you; keep them like any test log.
- **A hostile `scrimmage.toml`.** `function` and `command` name code that runs, so treat the file like code.
  A CI job that holds model API keys must not run `scrimmage test` on a pull request from an outside contributor.

[Limits](limits.md) lists every known gap of this version.

## Reporting a vulnerability

If you find a security problem in Scrimmage, please report it privately, not in a public issue.

!!! warning "Contact to be added"
    PLACEHOLDER: the private reporting channel is not set yet.
    The maintainer will add it here and in `SECURITY.md` before the first release.

Please include what you found, how to reproduce it, and the version or commit you tested.
