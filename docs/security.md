# Security

This page covers the trust boundaries of a test: where your data goes, how Scrimmage's processes talk to each other, what it keeps in records, and how to report a problem.
The rules a test applies call by call, such as which calls are refused, are on [Safety](safety.md).

Each statement below is enforced by code in the `scrimmage` package.
The line "Checked by" under it names the test in the repository that fails if the statement stops being true.

## Your data stays on your machine

Scrimmage has no telemetry and no hosted service, and it sends nothing to its authors.
It makes three kinds of connection, and no others: to its own control server, to the service address your `scrimmage.toml` names for a web service, and to GitHub when you run `scrimmage prices update`, which downloads one public file of model prices.
That command sends a GET request, so GitHub sees your IP address and the `User-Agent` of Python's urllib, and nothing about your agent, your tests or your data.
If `HTTPS_PROXY` is set, the request goes to that proxy, which sees the connection, and not straight to GitHub.
No test run makes that connection, and nothing else in Scrimmage does.
The fetch is over HTTPS to the repository's `main` and checks the server's certificate.
It follows no redirect to another host or to plain HTTP, and it stops after 15 seconds or 1 MB.
It is the only place Scrimmage reads from the network, and its module is loaded by no other command.
The only processes it starts are the runs of your agent, the test copy of a service that `start` names, and, in a service, Python's own resource tracker, once, in the process that serves the app or supervises its workers, before any request.
The Scrimmage MCP server, a command of its own, also starts the `scrimmage` command each of its tool calls runs; see [Driving Scrimmage from a coding agent](mcp-server.md).

*Checked by: `tests/test_security_audit.py::test_a_watched_run_talks_only_to_loopback_starts_only_its_program_and_never_sends_the_token`, `tests/test_security_claims.py::test_the_only_connections_are_to_the_control_server_and_the_service`, `tests/test_security_claims.py::test_only_the_prices_update_command_calls_the_one_fetch`, `tests/test_security_claims.py::test_loading_the_command_and_a_test_runs_modules_never_loads_the_fetch`, `tests/test_prices_update.py::test_a_redirect_is_refused_before_a_request_goes_to_the_other_place`*

Scrimmage's start-up inside the agent is checked by a source scan, not watched.
It runs before any watcher in that process could be installed, so the scan checks that no product module but the runner, its start of a service's test copy, `scrimmage-run` and the Scrimmage MCP server, a command of its own, starts a process, and that only the SDK's client opens a connection.

*Checked by: `tests/test_security_claims.py::test_the_only_processes_scrimmage_starts_are_its_runs`, `tests/test_security_claims.py::test_the_only_connections_are_to_the_control_server_and_the_service`*

The runner and the code inside your agent talk over one local connection, to a control server that listens on `127.0.0.1` only, on a port chosen for each test.
A [service in a container](docker.md#a-service-in-a-container) or in a [Kubernetes cluster](kubernetes.md) is the exception: there `control` names the address the runner listens on, and the channel runs over TLS.

*Checked by: `tests/test_security_claims.py::test_the_control_server_listens_on_loopback_only`*

Apart from its two optional extras, `scrimmage[mcp]` and `scrimmage[cert]`, the `scrimmage` package uses only Python's standard library, so it brings in no network client of another project.

*Checked by: `tests/test_stdlib_only.py::test_imports_only_the_standard_library`*

Your agent's own calls, such as model calls to your provider, go where your agent sends them, as they would without Scrimmage, or nowhere when a test mocks, faults or refuses them.

*Checked by: `tests/test_security_claims.py::test_no_fault_but_the_carried_ones_sends_a_request`, `tests/test_mcp.py::test_the_server_never_sees_a_mocked_or_faulted_call`*

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

## TLS on the control channel

A control server that listens beyond loopback carries plans and record lines, which can hold customer data, so it never does so in clear.

- A `control` address that is not loopback with no `control_tls` is refused when the file is read, exit 2.
  `control_tls` without `control` is refused too, and so is an address that listens on every interface or is a name.

  *Checked by: `tests/test_control_tls.py::test_control_beyond_loopback_without_control_tls_is_exit_2`, `tests/test_control_tls.py::test_control_tls_without_control_is_exit_2`, `tests/test_control_tls.py::test_control_on_an_unspecified_address_is_refused`, `tests/test_control_tls.py::test_control_must_be_an_ip_literal`*

- A service whose `SCRIMMAGE_CONTROL_URL` is `http://` beyond loopback stops at its start, and so does one with an `https://` address and no `SCRIMMAGE_CONTROL_CA`, or a file of several certificates.

  *Checked by: `tests/test_control_tls.py::test_http_beyond_loopback_stops_the_service_naming_https`, `tests/test_control_tls.py::test_https_without_scrimmage_control_ca_stops_the_service_naming_it`, `tests/test_control_tls.py::test_a_ca_file_with_two_certificates_stops_the_service`*

- The SDK trusts the one authority it is given.
  It refuses a certificate from another authority, one that an authority in the system's store signed, and one for another name.

  *Checked by: `tests/test_control_tls.py::test_the_sdk_refuses_a_certificate_not_from_the_given_authority`, `tests/test_control_tls.py::test_the_sdk_refuses_a_public_authority_and_never_loads_the_system_store`, `tests/test_control_tls.py::test_the_sdk_refuses_a_certificate_for_another_name`*

- The SDK speaks TLS 1.3 only and applies OpenSSL's strict chain rules, so Python 3.11 to 3.14 judge a chain alike.

  *Checked by: `tests/test_control_tls.py::test_the_sdk_refuses_tls_1_2`, `tests/test_control_tls.py::test_the_sdk_refuses_an_authority_that_only_strict_mode_rejects`*

- Every control request, whatever its kind, goes through the one TLS connection.

  *Checked by: `tests/test_control_tls.py::test_every_control_request_kind_goes_through_the_tls_connection`*

- The runner starts only with a private key in a plain file you own with mode `0600`, and a certificate that matches the key and is inside its dates.

  *Checked by: `tests/test_control_tls.py::test_a_control_key_not_0600_is_exit_2`, `tests/test_control_tls.py::test_an_expired_or_mismatched_certificate_is_exit_2_at_start`*

- The control server runs each connection's TLS handshake in a thread of its own, and cuts a connection after 10 seconds without a read or 15 seconds in all.
  A client that stalls its handshake or sends a byte at a time holds one connection until then and blocks no one else.
  A request that declares a body over 4 MB is read only after its signature is checked.

  *Checked by: `tests/test_control_tls.py::test_a_stalled_handshake_does_not_block_a_second_client_and_is_cut_at_the_deadline`, `tests/test_control_tls.py::test_a_slow_drip_is_cut_at_the_whole_request_deadline`, `tests/test_control_tls.py::test_an_unsigned_large_body_is_refused_before_it_is_read`*

- That check includes the time.
  A header the token made is good for 60 seconds, so one captured earlier and sent again, with a declared 14 MB body, is refused with `401 clock` before any of the body is read.
  The server reads at most 8 KiB ahead of the headers on every Python version.

  *Checked by: `tests/test_service_versions.py::test_a_signed_header_older_than_60_s_is_refused_before_any_body_is_read`*

- The service pings the control server before it answers a probe.
  The ping is signed like every request and its reply is signed for it, and it stores nothing.
  The control server answers with a random id made for this test, and the runner stops the test with exit 3 if the id is not its own, so a second runner that holds the same token cannot stand in for this one.

  *Checked by: `tests/test_service_versions.py::test_the_ping_stores_nothing_and_is_signed`, `tests/test_service_versions.py::test_a_ping_reply_with_a_bad_signature_is_not_taken`, `tests/test_service_versions.py::test_a_ping_answered_by_another_runner_stops_the_test`*

- A claim for a run's plan carries the SDK's version, and the control server refuses a claim from another version, or with no version, with `409 version`.
  The gate answers that run `503 version`, and the runner stops the test.

  *Checked by: `tests/test_service_versions.py::test_a_claim_with_no_sdk_field_is_refused`, `tests/test_service_versions.py::test_a_worker_of_another_version_is_refused_at_its_first_claim_and_stops_the_test`*

- A connection that fails TLS prints no traceback; the report counts it by OpenSSL's reason.
  The report also counts the runs it sent and the claims it received, so a control server that no service can reach shows on the runner.

  *Checked by: `tests/test_control_tls.py::test_tls_failures_are_counted_by_reason_and_print_no_traceback`, `tests/test_control_tls.py::test_the_report_counts_runs_sent_and_claims_received`*

- A four-worker service test over TLS on loopback completes, and each run's record holds only that run's lines.

  *Checked by: `tests/test_control_tls.py::test_a_full_s7a_run_works_over_tls_on_loopback`*

TLS protects what crosses the control channel against a reader on the network.
It adds nothing against someone who holds the token, and the HMAC stays on every request and reply.
The runner's requests to the service stay `http://`, and what that exposes is on [A service in Docker Compose](docker.md#a-service-in-a-container).

## A web service's token and run header

When Scrimmage [starts the service](services.md#let-scrimmage-start-the-service) with `start`, the token is one the runner makes for that test.
It is in a mode `600` file in a mode `700` folder the runner made, never in an environment variable, and the folder is deleted when the test ends.
A runner killed with SIGKILL cannot delete it; the folder then stays in the temporary folder, and its token opens nothing, as the control server it signs for has gone with that runner.
The copy's environment holds none of the runner's own `SCRIMMAGE_` variables, only the settings it needs.
On this path the control server listens on `127.0.0.1` only, over `http://`.

*Checked by: `tests/test_managed_start.py::test_the_token_file_is_private_and_gone_after`, `tests/test_managed_start.py::test_the_child_gets_the_settings_and_no_other_scrimmage_variable`*

When you [start the service yourself](services.md#attach-to-a-service-you-started), it runs before and after the test, so the token is yours, not one the runner makes.
Keep it as you keep a production secret.
The service and the runner read it from the file `SCRIMMAGE_TOKEN_FILE` names, which must be a plain file you own, with mode `0600`, and hold 32 characters or more.
A service with `SCRIMMAGE_TOKEN` in its environment stops, so no process environment holds the token.

*Checked by: `tests/test_service.py::test_the_token_file_must_be_0600_and_owned`, `tests/test_service.py::test_scrimmage_token_in_a_service_environment_stops_naming_the_file`, `tests/test_service.py::test_the_token_and_control_url_come_from_the_runners_environment`*

In a container the service must run as the file's owner, or it stops and names the file's user id and its own.

*Checked by: `tests/test_control_tls.py::test_a_token_file_owned_by_another_uid_names_the_user_setting`*

In a Kubernetes pod the token and the control key are files of a Secret volume, which the kubelet writes as root with the pod's `fsGroup` as group, mode `440`, on a read-only mount.
Scrimmage reads such a file only through the kubelet's own links, and only when root owns it, its group is the process's, no bit is set for others and the mount is read-only; it checks all of that on the open file.
Any one of the process's groups counts, so a group from the pod's `supplementalGroups` passes too.
Every container in the pod shares the group, so every container in the pod can read the token; the pod is the boundary, as it is for any Secret.
No other pod sees the volume.

*Checked by: `tests/test_secret_file.py::test_a_kubernetes_secret_file_is_accepted`, `tests/test_secret_file.py::test_a_link_out_of_the_folder_is_refused`, `tests/test_secret_file.py::test_a_secret_file_others_can_read_is_refused`, `tests/test_secret_file.py::test_all_three_readers_use_the_rule`*

Each process that serves the app removes the file's path from its own environment, so a program it starts never learns where the token is.
The token stays in that process's memory for the service's life, where any code in the process can read it.

*Checked by: `tests/test_service.py::test_a_token_file_path_is_popped_in_each_worker`, `tests/test_service.py::test_a_teams_spawn_pool_worker_started_from_a_worker_stays_off_and_runs`, `tests/test_service_gate.py::test_only_a_process_that_serves_the_app_pops_the_token_path_and_says_it_is_active`*

Each run's request carries one header, `x-scrimmage-run: run-<12 hex>.<16 hex>`, whose second part is an HMAC-SHA256 of the run's name with the token.
The service checks it locally, so a header made without the token gets `503 bad-run-header` before any request to the runner, and a flood of forged headers costs one HMAC each and never reaches the control server.

*Checked by: `tests/test_service_gate.py::test_a_header_with_a_wrong_mac_gets_503_with_no_control_request`, `tests/test_service_workers.py::test_a_header_flood_turns_runs_into_nothing_worse_than_infrastructure_errors`, `tests/test_service_workers.py::test_the_mac_check_costs_microseconds`*

A run is claimed once, by one process, and the runner takes that run's lines from that process only.
So a genuine header replayed, or forwarded by a proxy to another service, gets `503 claimed`, never a result.

*Checked by: `tests/test_service_gate.py::test_a_second_claim_of_a_run_gets_503_claimed`, `tests/test_service_gate.py::test_lines_from_a_process_that_did_not_claim_are_refused`*

The claiming process's last batch names the plan its run state used, as a digest.
The digest is HMAC-SHA256 keyed by the service token, over the plan without its `elapsed_s` and `scrub`, so a record on a volume does not let a reader test a guess at a mock's value.
A run whose digest differs from the runner's own is an infrastructure error, and a claimed run's last batch with no digest is refused, so that run is an infrastructure error too.
The service token is the one in the service's token file, which lasts across tests, so the token file must never sit on the records volume: whoever reads both can test guesses at the plan.

*Checked by: `tests/test_plan_digest.py::test_the_stored_digest_does_not_reveal_a_scrubbed_value`, `tests/test_plan_digest.py::test_a_run_whose_state_changed_its_plan_is_an_infrastructure_error`, `tests/test_plan_digest.py::test_a_claimed_runs_last_batch_with_no_digest_is_refused`*

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
| Someone who captured a genuine probe whose time was outside the 60 second window and replays it | a signed reply that says only that the clocks differ, once to each worker per 120 seconds | disclosed below |

Each worker keeps its own record of the nonces it has seen, so a probe captured on the loopback path can be replayed once to each other worker within 60 seconds, and gets that worker's startup report.
The report holds nothing secret, so this version does not share nonces across workers.

A probe whose signature and body are good but whose time is more than 60 seconds off the service's clock gets a signed `409` that holds no time.
Only a holder of the token can make such a probe, so a stranger gets nothing.
Someone who captured one genuine probe, such as a container with `CAP_NET_RAW` on the bridge, can replay it and learn only that Scrimmage is in the service.
Each worker keeps its own nonces and forgets one after 120 seconds, so a replay to another worker, or to the same worker after 120 seconds, gets the signed reply again.
A replay to the same worker within 120 seconds finds its nonce used and reaches the app as a plain request.

*Checked by: `tests/test_check_service.py::test_a_replayed_stale_probe_is_answered_as_documented`, `tests/test_check_service.py::test_a_probe_outside_the_window_gets_a_signed_clock_reply_with_no_time_and_exit_3_naming_ntp`*

## The control channel in a Kubernetes cluster

In a cluster the runner runs as a Job and the agent as a Deployment, set up as [In Kubernetes](kubernetes.md#in-kubernetes) shows.
The shipped network policies deny all traffic in the namespace, then allow four paths: the agent's pods to the runner on port 7700, the runner to the agent on port 8000, the agent to its model host, and every pod to cluster DNS.
The tests run Calico, which enforces them; a network plugin that does not enforce NetworkPolicy ignores them.

- Only the agent's pods reach the runner's control port.
  A pod in the namespace with another label gets no answer, and so does a pod in another namespace, even one with the agent's label.
  A pod in the namespace that no policy names cannot be reached from another namespace.

  *Checked by: `tests/test_k8s_security.py::test_only_the_agents_pods_reach_the_control_port`*

- The runner reaches only the agent and cluster DNS.
  It cannot reach the model host or any address outside the cluster.

  *Checked by: `tests/test_k8s_security.py::test_the_runner_reaches_only_the_agent_and_dns`*

- The runner's account has no Role and no RoleBinding, and no API token is mounted in its pod.
  It may do only what every account may, such as ask what it may do, so a runner that is taken over cannot call the Kubernetes API.

  *Checked by: `tests/test_k8s_security.py::test_the_runners_service_account_can_do_nothing`*

- The namespace enforces the Pod Security `restricted` standard, and every pod the manifests make meets it.
  Each runs as uid 10001, never as root, with no privilege escalation, every Linux capability dropped, the default seccomp profile and a read-only root file system.
  A pod that breaks the standard is refused when it is made, even with no Deployment around it.
  Dropping every capability drops `CAP_NET_RAW`, so no pod can read other pods' traffic off the network.

  *Checked by: `tests/test_k8s_security.py::test_every_pod_passes_the_restricted_standard`*

- In the default setup, `scrimmage cert` plus `kubectl create secret`, no authority key exists once `scrimmage cert` returns, and the authority is constrained to the three names given, `scrimmage`, `scrimmage.<namespace>.svc` and `scrimmage.<namespace>.svc.cluster.local`, and the names under them, and excludes every IP address.
  So nothing can sign another control certificate, and the SDK refuses a certificate for another namespace's control name, such as `scrimmage.other.svc`, and one for an address.

  *Checked by: `tests/test_cert.py::test_cert_writes_no_authority_key`, `tests/test_cert.py::test_the_authority_is_name_constrained_to_the_given_names_and_their_subdomains`, `tests/test_cert.py::test_an_authority_of_names_alone_refuses_every_address`, `tests/test_k8s_cluster.py::test_the_default_authority_refuses_a_name_outside_its_three`, `tests/test_k8s_cluster.py::test_the_default_certificates_come_from_scrimmage_cert_and_pass_the_handshake`*

- Each process in the agent's pods connects only to the Service `scrimmage` on port 7700 and to the model host, and looks up only those two names.

  *Checked by: `tests/test_k8s_security.py::test_the_agents_pods_connect_only_to_the_control_service_and_the_model`*

With the policies, the agent's pods and the processes on the node itself can reach port 7700; Calico lets a node reach its own pods, which the kubelet's probes need.
Without them, every pod and every node in the cluster can.

| Who | Can | What follows |
|---|---|---|
| A pod in another namespace, or one in this namespace with another label | connect to port 7700 | nothing: the policy drops the packets. A client that holds the control server's connections needs a connection first, so it is shut out |
| Anyone who can make a pod with the agent's label in the namespace, directly or through any workload | reach port 7700 | what any client without the token gets: refused signatures, at most 4 MB read before the check, 15 seconds a connection. Such a pod can also mount the token Secret, which is the boundary row below |
| A pod on the pod network with `CAP_NET_RAW` | read other pods' traffic | nothing: Calico gives each pod a link of its own, with no shared bridge, and `restricted` drops the capability anyway |
| Root on a node, or a pod on the host's network | read the runner's requests to the agent, which are `http://`, and the records on the volume | everything, as on any node. This is out of scope |
| Anyone who can make a Service named `scrimmage` in the namespace | answer the agent's control requests | its certificate fails the check, and the agent sends nothing |
| Anyone who can read Secrets, or create or edit workloads (pods, Deployments, Jobs and the like), in the namespace | the token and the control server's key: a pod they start mounts any Secret of the namespace, with no RBAC on Secrets | everything. **This is the boundary.** Grant those rights in a test's namespace only to those you would give the token |
| A runner pod that is taken over | call the Kubernetes API | nothing: no token is mounted, and its egress reaches only DNS and the agent |
| A process in the agent's pods | reach any other address | the egress policy drops it. A process can still put data in the names it looks up, which cluster DNS passes on; see [Limits](limits.md#in-a-kubernetes-cluster) |

The runner's requests to the agent stay `http://`, so request bodies, the `headers` from the file and every answer cross the pod network in clear.
On one node that path is inside the node; between nodes Calico carries it unencrypted unless its WireGuard encryption is on.

### With cert-manager

[With cert-manager](kubernetes.md#with-cert-manager), the option for a team that already runs it, the authority keeps its key in `scrimmage-pki` and an Issuer there signs every request it gets.

- The control authority carries a critical name constraint that permits `scrimmage`, `svc` and `cluster.local`, and the names under them, and excludes every IP address.
  The authority's Issuer still signs any name it is asked for, but the SDK refuses a certificate for a name outside the constraint, such as `evil.example.com`, and one for an address, such as `10.96.0.10`.

  *Checked by: `tests/test_k8s_cluster.py::test_the_authority_signs_only_names_under_its_constraints`*

| Who | Can | What follows |
|---|---|---|
| Anyone who can create a cert-manager `Certificate` or `CertificateRequest` in `scrimmage-pki`, the namespace of the authority and its Issuer | have the authority sign a certificate for `scrimmage` | a certificate the agent accepts, so they can answer as the runner, though with no token they sign no plan. The agent refuses one for a name outside the authority's constraint or for an IP address. cert-manager approves every request by default, so this is a real path. cert-manager's approver-policy project is the production hardening; the tests do not run it |

The authority's key lives only in `scrimmage-pki`, and no test namespace holds an Issuer, so rights in a test's namespace are not rights to sign.

## The plan is data

For each run, the runner sends the code inside your agent a plan: which calls to mock, which to fail, and which may run for real.
The plan is data only.
It holds no code.

Three settings name Python objects: a mock's `type` and `returns_from`, and a fault's `raises`.
Scrimmage imports such a name only if it is under one of your agent's own top-level packages, the ones that `function` and `tools` name.
The check runs when the file loads, at the start of each process, and again before the import.
So a plan that names anything else stops the program before your agent runs.

A `returns_from` function is your code, and it runs inside your agent's process at each mocked call.
That is the trust a `type` already has: its class's own code runs when the value is built.
The plan carries only the function's name.
Calls the function makes pass the gates like any other code in the run.
If it raises, even `SystemExit`, the record names the function and the error's class, never the error's message, which can quote a value.
A mock that its class rejects is a mistake that can name a field, or a key from the mock's own table, and never a value.
For a mock you wrote, that key is yours.
For a recorded mock, the table's keys came from the real reply.
That holds for an Enum too: the mistake names the class and its members, not the value given.
The reason that a reply cannot be saved as a mock names a dict entry of the reply by position, never by its key.
A position is written in braces, such as `returns{2}{0}`, so it is not taken for a list index like `returns[2]`.
The top level of a call's arguments is named, as those keys are your tool's own parameter names.
Besides that mistake, three places in a recording can still name a key from a real reply.
`capture.differ` says where a rebuilt mock differs from the reply, and its text can reach the terminal.
The reason for a JSON text inside a reply that holds a secret names the secret-shaped key, such as `password`.
The record line's `result.preview` holds the reply as it was, under pattern redaction only, as "Records hold your data as it was" says below.
None of them names a value.

*Checked by: `tests/test_bootstrap.py::test_a_type_outside_the_allowed_prefixes_is_refused_before_import`, `tests/test_bootstrap.py::test_a_returns_from_outside_the_allowed_prefixes_stops_the_program_before_the_agent_runs`, `tests/test_tool_gate.py::test_a_type_outside_the_allowed_prefixes_is_refused_before_import`, `tests/test_schemas.py::test_the_sdk_rejects_a_bad_plan`, `tests/test_returns_from.py::test_a_factory_outside_the_allowed_imports_is_never_imported`, `tests/test_returns_from.py::test_a_factory_that_raises_is_a_mistake_naming_it_and_not_its_message`, `tests/test_mock_type.py::test_a_value_the_class_rejects_is_in_no_report_record_or_log`, `tests/test_mock_type.py::test_an_enum_value_that_is_no_member_is_in_no_report_record_or_log`, `tests/test_capture.py::test_an_unsavable_reason_names_a_real_dict_key_by_position_never_by_name`, `tests/test_capture.py::test_an_argument_is_named_at_the_top_and_by_position_below_it`*

## A fault sends nothing

Only `append`, `replace` and `slow` let the call they name happen.
Every other fault is built by the gate, and nothing is sent: no request reaches a server or a provider, no tool runs, and a model call reserves and costs nothing.

*Checked by: `tests/test_security_claims.py::test_no_fault_but_the_carried_ones_sends_a_request`, `tests/test_connection_faults.py::test_a_connection_fault_on_a_model_call_reserves_and_costs_nothing`*

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

- Calls that cost more than their reservation, a reply past its output limit or an input with more tokens than the estimate, can together pass the cap by what they cost above it; see [A call can cost more than its hold](limits.md#the-budget).
- A provider that is neither OpenAI's nor Anthropic's format, listed under `real` as a web host, is not budgeted at all.
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
- **A provider that is neither OpenAI's nor Anthropic's format.** Listing its host under `real` lets its calls through as web requests, which no budget counts.
- **Calls Scrimmage does not see.** A program on another Python, or a Python child started through a shell, is not gated.
- **Secrets of an unknown shape.** Redaction removes the shapes listed above only.
  A `password: hunter2` with a colon, a `pass=hunter2` and a bare `pw is hunter2` are recorded as written.
  Pass secrets in the environment, which is never recorded.

  *Checked by: `tests/test_security_claims.py::test_a_secret_with_no_known_shape_passes_through_redaction`*

- **Your agent's own output.** The per-run `.log` and `.out` files hold whatever your agent printed, and they are not cleaned, except of the values the file reads with `${NAME}`.
  They are readable only by you; keep them like any test log.
- **A hostile `scrimmage.toml`.** `function`, `command` and `returns_from` name code that runs, so treat the file like code.
  A CI job that holds model API keys must not run `scrimmage test` on a pull request from an outside contributor.

[Limits](limits.md) lists every known gap of this version, and [Safety](safety.md) the rules each gate applies.

## Who Scrimmage defends against

Scrimmage guards honest team code that uses public APIs, and the internals these docs name.
Code that rewrites the private parts of Python's objects or of Scrimmage's own is out of scope.
That includes a `multiprocessing` queue's `_buffer`, `_writer` or `_send_bytes`, a pool's private queues, and Scrimmage's own state.
Such code can get around a gate, and no fix is promised for it.
Public APIs stay in scope, such as `ForkingPickler.register` and `copyreg`.

## Reporting a vulnerability

If you find a security problem in Scrimmage, please report it privately, not in a public issue.

Report it through GitHub's private vulnerability reporting.
On the repository's Security tab, choose "Report a vulnerability".

Please include what you found, how to reproduce it, and the version or commit you tested.
`SECURITY.md` in the repository says how a report is handled.
