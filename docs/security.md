# Security

This page covers the trust boundaries of a test: where your data goes, how Scrimmage's processes talk to each other, what it keeps in records, and how to report a problem.
The rules a test applies call by call, such as which calls are refused, are on [Safety](safety.md).

Each statement below is enforced by code in the `scrimmage-core` package.
The line "Checked by" under it names the test in the repository that fails if the statement stops being true.

## Your data stays on your machine

Scrimmage has no telemetry and no hosted service.
It sends nothing to its authors or to any third party.
The only connections Scrimmage itself makes are to its own control server, and the only processes it starts are the runs of your agent, and, in a service, Python's own resource tracker, once, in the process that serves the app or supervises its workers, before any request.
The Scrimmage MCP server, a command of its own, also starts the `scrimmage` command each of its tool calls runs; see [Driving Scrimmage from a coding agent](mcp-server.md).

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

## A web service's token and run header

When the agent is [inside a web service](services.md), the service runs before and after the test, so the token is yours, not one the runner makes.
Keep it as you keep a production secret.
The service and the runner read it from the file `SCRIMMAGE_TOKEN_FILE` names, which must be a plain file you own, with mode `0600`, and hold 32 characters or more.
A service with `SCRIMMAGE_TOKEN` in its environment stops, so no process environment holds the token.

*Checked by: `tests/test_service.py::test_the_token_file_must_be_0600_and_owned`, `tests/test_service.py::test_scrimmage_token_in_a_service_environment_stops_naming_the_file`, `tests/test_service.py::test_the_token_and_control_url_come_from_the_runners_environment`*

Each process that serves the app removes the file's path from its own environment, so a program it starts never learns where the token is.
The token stays in that process's memory for the service's life, where any code in the process can read it.

*Checked by: `tests/test_service.py::test_a_token_file_path_is_popped_in_each_worker`, `tests/test_service.py::test_a_teams_spawn_pool_worker_started_from_a_worker_stays_off_and_runs`, `tests/test_service_gate.py::test_only_a_process_that_serves_the_app_pops_the_token_path_and_says_it_is_active`*

Each run's request carries one header, `x-scrimmage-run: run-<12 hex>.<16 hex>`, whose second part is an HMAC-SHA256 of the run's name with the token.
The service checks it locally, so a header made without the token gets `503 bad-run-header` before any request to the runner, and a flood of forged headers costs one HMAC each and never reaches the control server.

*Checked by: `tests/test_service_gate.py::test_a_header_with_a_wrong_mac_gets_503_with_no_control_request`, `tests/test_service_workers.py::test_a_header_flood_turns_runs_into_nothing_worse_than_infrastructure_errors`, `tests/test_service_workers.py::test_the_mac_check_costs_microseconds`*

A run is claimed once, by one process, and the runner takes that run's lines from that process only.
So a genuine header replayed, or forwarded by a proxy to another service, gets `503 claimed`, never a result.

*Checked by: `tests/test_service_gate.py::test_a_second_claim_of_a_run_gets_503_claimed`, `tests/test_service_gate.py::test_lines_from_a_process_that_did_not_claim_are_refused`*

Every `x-scrimmage-` header is removed before your app sees the request, so the app cannot pass the header on.
The MAC covers the run's name, not the body, so the gate never holds a request's body.

*Checked by: `tests/test_service_gate.py::test_the_run_header_never_reaches_the_app`, `tests/test_service_gate.py::test_a_probe_that_declares_a_body_goes_to_the_app_unread`*

The probe's reply is signed with the token over the probe's own signature, so a server without the token cannot pass for a gated service.
The report it holds has versions, gate names and tool names: no token, no plan and nothing of a run.

*Checked by: `tests/test_service.py::test_a_probe_reply_with_a_forged_signature_stops_the_runner`, `tests/test_service.py::test_a_probe_signed_with_another_token_reaches_the_app_and_the_runner_stops`, `tests/test_service_gate.py::test_the_probes_report_with_a_notice_matches_its_schema`*

Each value the file reads with `${NAME}`, of 8 characters or more, is removed from every record line, side-folder item, summary, report, `result.json` and the run's `.log` and `.out` files, as written, JSON-escaped, URL-encoded and in base64.

*Checked by: `tests/test_service_config.py::test_an_expanded_value_never_reaches_a_record_a_blob_the_summary_or_result_json`, `tests/test_service_config.py::test_a_scrub_value_is_removed_json_escaped_url_encoded_and_in_base64_at_each_offset`, `tests/test_scripts.py::test_a_runs_log_is_scrubbed_even_when_the_runs_end_fails`*

| Who | What they can do | What follows |
|---|---|---|
| A client without the token that sends a run header | nothing: the MAC fails in the gate | `503 bad-run-header`, no control request |
| Someone on the loopback path who reads a genuine test request | resend its header | `503 claimed`: the run is claimed already. Never a result |
| The app, or a proxy, that forwards incoming headers downstream | nothing: the header is gone before the app sees it | a downstream gated service would get `503 claimed` |
| Code inside the service's process | read the token from memory | the same as any code in the agent, as for a script |
| Someone on the path who replays a genuine probe to another worker within 60 seconds | that worker's startup report | disclosed below |

Each worker keeps its own record of the nonces it has seen, so a probe captured on the loopback path can be replayed once to each other worker within 60 seconds, and gets that worker's startup report.
The report holds nothing secret, so this version does not share nonces across workers.

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
    - bearer tokens: `Bearer` and the value after it, all of it; a key shape or a field name that starts inside the value is read again, so in `Bearer abc...Authorization: Basic ...` the Basic credential goes too;
    - a Basic credential: `Authorization` and `Basic` and a value of 8 or more characters after them (a shorter word, as in `Authorization: Basic realm`, is kept), in a header line, in JSON (as a header map, a list of pairs, or a name and a value) or in a command line;
    - form-encoded and keyword fields: `password=`, `passwd=`, `token=`, `secret=` and `api_key=` and the value after them, with any `NAME_` before it, as in `DB_PASSWORD=`, `GITHUB_TOKEN=`, `AWS_SECRET_ACCESS_KEY=` and `client_secret=`, in any letter case, with `=` written as `%3D` and `_` in `api_key` as `%5F`; the value may be quoted with `'` or `"`, or in `<...>`; the env names `PGPASSWORD`, `PGPASS`, `MYSQL_PWD`, `authtoken` and `accesstoken` are removed too;
    - JSON Web Tokens, which start with `eyJ`.

  *Checked by: `tests/test_security_claims.py::test_each_listed_secret_shape_is_removed`, `tests/test_redact.py::test_a_jwt_is_removed_after_any_separator`, `tests/test_redact.py::test_the_old_module_kept_each_new_secret_and_the_new_one_removes_it`, `tests/test_redact.py::test_a_marker_is_removed_with_what_sticks_to_it`, `tests/test_record.py::test_a_json_reply_holding_a_form_field_in_a_string_is_written_as_valid_json`, `tests/test_redact.py::test_what_starts_inside_a_bearer_token_is_read_again`, `tests/test_redact.py::test_a_bearer_token_shields_no_secret_glued_after_it`*

  A form field with an empty value, prose such as "reset your password at", a URL path that holds the word `token`, and these names are left as written: the paging names (`next_page_token`, `page_token`), anything after `is_`, `has_`, `top_`, `max_`, `min_`, `num_` or `n_`, the tokenizer names (`eos_token`, `bos_token`, `pad_token`, `unk_token`, `sep_token`, `cls_token`) and a name glued to a word, such as `mytoken`.
  `None`, `True` and `False` alone as a value, and a bare name as an argument of a call that has a name, such as `f(token=tok)`, are code and are left as written.
  A value kept this way ends where a field name or a key shape begins in it, and the rest is read again by every rule: in `next_page_token=ab,password=x` the paging token stays and the password is removed, and in `f(token=ghp_...)` the GitHub token is removed.
  For a text that is JSON as a whole, a field name at the end of a string cannot run into the next string: the JSON object, array or string is read first and each string in it is cleaned on its own, so the result still parses, with the same keys and list lengths, and only a change is written back.
  In JSON a name and its value are two strings, so `Authorization` with a Basic value is judged as a pair.
  One rule does this for JSON text and for Python values, so a mock and a tool value are cleaned the same way.
  It reads a header map (the value may be a list, and an object beside the Basic value in that list is cleaned as usual), and an object with a label (`name`, `key`, `header` or `field`) set to Authorization and a `value` or `values` beside it, in any letter case, so .NET's `{"Key": ..., "Value": ...}` is read.
  It also reads a name followed by a value in any list, as in `["Host", "x", "Authorization", "Basic ..."]`, with the value one list deeper if need be, and a name with a trailing colon.
  The name may end in `authorization` after `_` or `-`, as in `HTTP_AUTHORIZATION` and `X-Forwarded-Authorization`.
  Rule names in messages: `openai-key`, `aws-access-key`, `google-api-key`, `github-token`, `slack-token`, `bearer-token`, `basic-credential`, `form-field`, `header-text`, `url`, `jwt`, `json`, `secret`.
  A refusal that says a change touches text a rule removes names one of these.
  Which rule runs where: the patterns above run on all recorded text.
  The key-name rule, which replaces a string under a key such as `"password"`, runs for mocks (web JSON replies, tool values and MCP values) and for miss evidence, and it reports what it removed.
  The side folder of blobs and the `.log` previews pass through the patterns only, so a JSON key such as `"password": "x"` stays there word for word.
  A `\u` escape is decoded before the check, so `p\u0061ssword=x` in JSON is found.
  In text that is not valid JSON a raw `\uXXXX` escape is a break before a field name, as a space is, so `psql\u0020password=x` loses the password; an escape inside a name there is not read as a letter, as [Limits](limits.md) says.
  A URL-encoded comma, `&`, `;`, space or `|` (`%2C`, `%26`, `%3B`, `%20`, `%7C`) is a break too; an encoded letter, as in `%70assword=x`, is not.
  If writing it back fails, the whole text is replaced by a JSON string `"[secret removed]"` and never left broken.

  *Checked by: `tests/test_redact.py::test_redacted_json_parses_with_the_same_structure_and_no_seeded_secret`, `tests/test_redact.py::test_a_text_that_parsed_as_json_still_parses_if_writing_it_back_fails`, `tests/test_redact.py::test_json_with_a_field_name_at_the_end_of_a_string_keeps_its_strings_apart`, `tests/test_security_claims.py::test_names_that_are_not_credentials_are_kept`, `tests/test_redact.py::test_prose_empty_values_and_paths_are_kept`, `tests/test_redact.py::test_a_name_and_value_that_are_two_strings_in_json_are_judged_together`, `tests/test_redact.py::test_numbers_spacing_and_unicode_are_written_back_as_they_were`, `tests/test_redact.py::test_a_basic_header_is_removed_from_python_values_and_from_json_text`, `tests/test_redact.py::test_the_same_pair_judgement_holds_for_python_values_and_json_text`, `tests/test_redact.py::test_a_json_key_named_password_is_left_to_the_recording_layer`, `tests/test_redact.py::test_an_object_in_a_header_list_is_cleaned_and_keeps_its_numbers`, `tests/test_redact.py::test_objects_beside_a_basic_value_and_pascal_case_labels_on_both_paths`, `tests/test_record.py::test_a_basic_header_in_a_web_reply_or_a_tool_value_is_not_written`, `tests/test_redact.py::test_only_none_true_false_and_a_bare_argument_of_a_named_call_are_code`, `tests/test_redact.py::test_a_kept_value_ends_where_a_field_or_a_key_shape_begins_in_it`, `tests/test_redact.py::test_no_kept_value_shields_a_secret_after_an_escape_and_any_glue`, `tests/test_redact.py::test_no_secret_hides_behind_an_escaped_or_encoded_separator`, `tests/test_redact.py::test_a_name_begins_after_a_raw_unicode_escape`, `tests/test_redact.py::test_a_name_begins_after_a_url_encoded_separator`, `tests/test_redact.py::test_a_name_after_a_bare_comma_in_a_named_call_is_not_code`*

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

## Who Scrimmage defends against

Scrimmage guards honest team code that uses public APIs, and the internals these docs name.
Code that rewrites the private parts of Python's objects or of Scrimmage's own is out of scope.
That includes a `multiprocessing` queue's `_buffer`, `_writer` or `_send_bytes`, a pool's private queues, and Scrimmage's own state.
Such code can get around a gate, and no fix is promised for it.
Public APIs stay in scope, such as `ForkingPickler.register` and `copyreg`.

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

- **Your agent's own output.** The per-run `.log` and `.out` files hold whatever your agent printed, and they are not cleaned, except of the values the file reads with `${NAME}`.
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
