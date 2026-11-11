# Security

This page covers the trust boundaries of a test: where your data goes, how Scrimmage's processes talk to each other, what it keeps in records, and how to report a problem.
The rules a test applies call by call, such as which calls are refused, are on [Safety](safety.md).

Each statement below is enforced by code in the `scrimmage-core` package.
The line "Checked by" under it names the test in the repository that fails if the statement stops being true.

## Your data stays on your machine

Scrimmage has no telemetry and no hosted service.
It sends nothing to its authors or to any third party.
The only connections Scrimmage itself makes are to its own control server, and the only processes it starts are the runs of your agent.

*Checked by: `tests/test_security_audit.py::test_a_watched_run_talks_only_to_loopback_starts_only_its_program_and_never_sends_the_token`, `tests/test_security_claims.py::test_the_only_connections_are_to_and_from_the_control_server`*

Scrimmage's start-up inside the agent is checked by a source scan, not watched.
It runs before any watcher in that process could be installed, so the scan checks that no product module but the runner and `scrimmage-run` starts a process, and that only the SDK's client opens a connection.

*Checked by: `tests/test_security_claims.py::test_the_only_processes_scrimmage_starts_are_its_runs`, `tests/test_security_claims.py::test_the_only_connections_are_to_and_from_the_control_server`*

The runner and the code inside your agent talk over one local connection, to a control server that listens on `127.0.0.1` only, on a port chosen for each test.

*Checked by: `tests/test_security_claims.py::test_the_control_server_listens_on_loopback_only`*

The `scrimmage-core` package uses only Python's standard library, so it brings in no network client of another project.

*Checked by: `tests/test_stdlib_only.py::test_imports_only_the_standard_library`*

Your agent's own calls, such as model calls to your provider, go where your agent sends them, as they would without Scrimmage.

## The control channel

The runner makes a new random value for each `scrimmage test`, 256 bits long, called the run token.
It passes the run token to the agent's processes in the `SCRIMMAGE_TOKEN` environment variable.
Each request to the control server, and each reply, is signed with HMAC-SHA256.
HMAC-SHA256 is a standard way to prove that a message came from someone who holds a shared secret; AWS signs its API requests with it, and Stripe its webhooks.

- The run token itself is never sent.
  A request's signature covers its method, path, time, a one-time nonce and a hash of its body.

  *Checked by: `tests/test_security_audit.py::test_a_watched_run_talks_only_to_loopback_starts_only_its_program_and_never_sends_the_token`*

- A request whose time is more than 60 seconds from the server's clock is refused.

  *Checked by: `tests/test_protocol.py::test_a_request_more_than_a_minute_off_is_refused`*

- A nonce used before is refused, so a request cannot be replayed.

  *Checked by: `tests/test_protocol.py::test_a_replayed_request_is_refused`*

- Each reply is signed over the request's own signature, its status and a hash of its body, so a reply cannot be moved to another request.

  *Checked by: `tests/test_protocol.py::test_a_reply_made_for_another_request_is_refused`*

What the signing protects against is limited.
Any process that runs as the same user can read the run token, for example from `/proc/<pid>/environ` on Linux.
So can any code inside the agent under test, which needs it so that its child processes can join the run.
The signing keeps out programs of other users on the machine only.
It follows that records are not tamper-proof against the agent under test: code in the agent could send lines of its own.
Do not use records as audit evidence.

## The plan is data

For each run, the runner sends the code inside your agent a plan: which calls to mock, which to fail, and which may run for real.
The plan is data only.
It holds no code.

Two settings name Python objects: a mock's `type` and a fault's `raises`.
Scrimmage imports such a name only if it is under one of your agent's own top-level packages, the ones that `function` and `tools` name.
The check runs before the import, and at the start of each process, so a plan that names anything else stops the program before your agent runs.

*Checked by: `tests/test_bootstrap.py::test_a_type_outside_the_allowed_prefixes_is_refused_before_import`, `tests/test_tool_gate.py::test_a_type_outside_the_allowed_prefixes_is_refused_before_import`, `tests/test_schemas.py::test_the_sdk_rejects_a_bad_plan`*

## Records

Every gated call is written to a record in `scrimmage-runs/`, next to your `scrimmage.toml`.

- The record folders are readable only by you: `scrimmage-runs/` and every folder in it have mode `0700`, and every file mode `0600`.
  Scrimmage writes a `.gitignore` there, so records are not committed by accident.

  *Checked by: `tests/test_security_claims.py::test_the_record_root_is_owner_only_even_if_it_was_open`, `tests/test_runner.py::test_every_record_file_is_owner_only`, `tests/test_runner.py::test_records_stay_out_of_the_teams_git_repo`*

- A web request's line holds its method and its path.
  Its headers, its cookies and its query string are never recorded.

  *Checked by: `tests/test_web_requests.py::test_no_header_cookie_or_query_value_reaches_the_record`, `tests/test_http_gate.py::test_a_query_string_never_reaches_the_record`*

- The environment is never recorded, and neither is the run token.

  *Checked by: `tests/test_security_audit.py::test_a_watched_run_talks_only_to_loopback_starts_only_its_program_and_never_sends_the_token`*

- Text that is recorded, such as tool arguments and results, model requests and replies, error messages, answers and command lines, has these secret shapes replaced with `[secret removed]`:
    - OpenAI and Anthropic API keys: `sk-`, `sk-proj-`, `sk-ant-`, `sk-svcacct-`;
    - AWS access key ids: `AKIA`, and `ASIA` for temporary ones;
    - Google API keys: `AIza`;
    - GitHub tokens: `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` and `github_pat_`;
    - Slack tokens: `xoxa-`, `xoxb-`, `xoxe-`, `xoxp-`, `xoxr-`, `xoxs-`, `xoxe.xoxp-` and `xapp-`;
    - bearer tokens: `Bearer` and the value after it;
    - a Basic credential: `Authorization` and `Basic` and the value after them, in a header line, in JSON or in a command line;
    - form-encoded fields: `password=`, `passwd=`, `token=`, `access_token=`, `secret=` and `api_key=` and the value after them, in any letter case, with `=` written as `%3D` or `_` in `api_key` as `%5F`;
    - JSON Web Tokens, which start with `eyJ`.

  *Checked by: `tests/test_security_claims.py::test_each_listed_secret_shape_is_removed`, `tests/test_redact.py::test_a_jwt_is_removed_after_any_separator`, `tests/test_redact.py::test_the_old_module_kept_each_new_secret_and_the_new_one_removes_it`, `tests/test_redact.py::test_the_only_difference_from_the_old_module_is_the_new_secrets_removed`*

  A form field with an empty value, prose such as "reset your password at", and a URL path that holds the word `token` are left as written.

  *Checked by: `tests/test_redact.py::test_prose_empty_values_and_paths_are_kept`*

- In any URL that is recorded, the query, the fragment and a user name or password are replaced with `[removed]`.

  *Checked by: `tests/test_redact.py::test_a_url_keeps_its_place_but_not_its_query_or_credentials`*

These are pattern matches, a heuristic: they remove secrets of a known shape only.
See the limits below.

Records hold your data as it was.
Tool results, model requests and replies in `scrimmage-runs/<time>/blobs/` keep customer and personal data word for word, apart from the secret shapes above.
Scrimmage never deletes records.
Delete old ones yourself, and keep them where you would keep that data.

## Real spending has a cap, and the cap is soft

Model calls run for real, so a test has a budget, $5 unless you change `max_cost_usd`.

- Each model call reserves an estimate of its cost before it is sent.
  A call that does not fit in what is left is not sent.

  *Checked by: `tests/test_budget.py::test_a_budget_that_cannot_cover_a_call_stops_the_test`*

- If the runner cannot be reached to check the budget, the call is refused rather than sent.

  *Checked by: `tests/test_http_gate.py::test_an_unreachable_control_server_refuses_the_call`*

The cap can still be passed:

- The estimate assumes `max_tokens`, or `reserve_output_tokens` when a request sets none.
  A reply that uses more output than that costs more than was reserved, so one call can take the total over the limit.
- A provider that is not OpenAI's format, listed under `real` as a web host, is not budgeted at all.
- A run killed with SIGKILL can leave a reservation that never settles.
  It counts as spent, so the cap errs low, not high.

See [Failing the model, and keeping a budget](model-faults.md).

## Staying out of production

- Installing or importing Scrimmage switches nothing on.

  *Checked by: `tests/test_bootstrap.py::test_importing_the_sdk_never_switches_gates_on`, `tests/test_bootstrap.py::test_without_the_token_nothing_is_imported_or_patched`*

- Its startup file is never on Python's default path.
  Only `scrimmage-run`, which `scrimmage test` uses for every run, puts it there.

  *Checked by: `tests/test_bootstrap.py::test_scrimmage_run_puts_its_folder_first`*

- The code inside your agent switches on only when a test run gives it the run token.
  If it then cannot set up every gate, the program stops before your agent runs, rather than running without them.

  *Checked by: `tests/test_bootstrap.py::test_a_broken_control_reply_stops_the_program_instead_of_running_ungated`*

## What Scrimmage does not protect against

- **Code written to get around it.** The gates run inside your program.
  They stop accidents, not code that sets out to evade them, such as calling a tool's plain function directly or patching a library.
  A test is not a sandbox.
  If one stray call would do real harm, make that system unreachable from the test environment, with test credentials or a network that cannot reach it.
- **A read that writes.** A `GET` or `HEAD` that is not mocked runs for real.
  A service that changes data on a `GET` is not caught.
  Mock it.
- **Code that is not a gated call.** Only listed tools, LangChain tools, httpx and httpx2 requests, MCP tool calls and model calls are gated.
  A database driver, boto3 and other cloud SDKs, urllib3, `requests`, gRPC or a raw socket inside code that is not a listed tool runs untouched and is not recorded.
  A listed tool that is neither mocked nor under `real` never runs, so a write inside it never happens.
- **MCP requests that are not tool calls.** `resources/read`, `prompts/get` and the other requests that are not tool calls pass, and a server can act on them.
  What a stdio MCP server does by itself, such as writing to its own database, is not gated.
- **A provider that is not OpenAI's format.** Listing its host under `real` lets its calls through as web requests, which no budget counts.
- **Calls Scrimmage does not see.** A program on another Python, or a Python child started through a shell, is not gated.
- **Secrets of an unknown shape.** Redaction removes the shapes listed above only.
  A `password: hunter2` with a colon, a `pass=hunter2` and a bare `pw is hunter2` are recorded as written.
  Pass secrets in the environment, which is never recorded.

  *Checked by: `tests/test_security_claims.py::test_a_secret_with_no_known_shape_passes_through_redaction`*

- **Your agent's own output.** The per-run `.log` and `.out` files hold whatever your agent printed, and they are not cleaned.
  They are readable only by you; keep them like any test log.
- **A hostile `scrimmage.toml`.** `function` and `command` name code that runs, so treat the file like code.
  A CI job that holds model API keys must not run `scrimmage test` on a pull request from an outside contributor.

[Limits](limits.md) lists every known gap of this version, and [Safety](safety.md) the rules each gate applies.

## Reporting a vulnerability

If you find a security problem in Scrimmage, please report it privately, not in a public issue.

!!! warning "Contact to be added"
    PLACEHOLDER: the private reporting channel is not set yet.
    The maintainer will add it here and in `SECURITY.md` before the first release.

Please include what you found, how to reproduce it, and the version or commit you tested.
`SECURITY.md` in the repository says how a report is handled.
