# An agent inside a web service

Many agents answer inside a web service, one request per question, beside every other request the service serves.
Scrimmage can test such an agent where it runs: you start the service once, and each run of a test is one `POST` to it.
This version covers ASGI services under uvicorn, such as FastAPI and Starlette apps.
A worked example is [An agent inside a web service](examples/service-agent.md).

## Start the service

The service and the runner share two things: a token, in a file only you can read, and the address of the runner's control server.

```bash
python -c "import secrets; print(secrets.token_hex(32))" > .scrimmage-token
chmod 600 .scrimmage-token
export SCRIMMAGE_TOKEN_FILE=$PWD/.scrimmage-token SCRIMMAGE_CONTROL_URL=http://127.0.0.1:8766
SCRIMMAGE_SERVICE=1 SCRIMMAGE_TOOLS=shop.tools:get_order SCRIMMAGE_ALLOW_IMPORTS=shop. \
  scrimmage-run uvicorn shop.service:app --port 8765 --loop asyncio
```

`--loop asyncio` is needed on Python 3.11 when uvloop is installed, as `uvicorn[standard]` installs it: on 3.11 Scrimmage cannot watch uvloop's tasks, so the service stops at its start and says so.
From Python 3.12 it can be left out.

Three variables switch Scrimmage on in the service:

| Variable | What it holds |
|---|---|
| `SCRIMMAGE_SERVICE` | `1`: Scrimmage runs in service mode. Without it, a token file alone stops the service with a message naming `SCRIMMAGE_SERVICE=1`. |
| `SCRIMMAGE_TOKEN_FILE` | The path of the token file: a plain file you own, mode `600`, or a Kubernetes Secret volume file (see [the token in a pod](kubernetes.md#the-token-and-the-key-in-a-kubernetes-pod)), with a token of 32 characters or more. Any other mode stops the service. |
| `SCRIMMAGE_CONTROL_URL` | The runner's control server: `http://127.0.0.1:port` when the runner is on this machine, or `https://host:port` for a runner the service reaches over a network. `http://` on any other host stops the service. |

An `https://` control address also needs one more variable:

| Variable | What it holds |
|---|---|
| `SCRIMMAGE_CONTROL_CA` | The authority to trust: a PEM file with exactly one certificate, the `ca.pem` that `scrimmage cert` writes. A file of several certificates, such as a system bundle, stops the service. |

Two more name what the gates swap, and `scrimmage check` prints both, built from your file:

| Variable | What it holds |
|---|---|
| `SCRIMMAGE_TOOLS` | The tools under `[agent] tools`, comma-separated, as the file lists them. |
| `SCRIMMAGE_ALLOW_IMPORTS` | The packages a plan may import a mock's type or a fault's exception from. |

`SCRIMMAGE_TOKEN` must not be set in a service's environment: the service stops and says to use the file, so no process environment holds the token.
`scrimmage-run` keeps its usual meaning: it puts Scrimmage's startup file first, then becomes uvicorn.
Each process that loads the app prints `scrimmage: active in service mode (pid N)` once, and then serves as it always does.
That process then removes `SCRIMMAGE_TOKEN_FILE` from its own environment, so a program it starts never learns where the token is.

The runner reads the same two variables, `SCRIMMAGE_TOKEN_FILE` and `SCRIMMAGE_CONTROL_URL`, from its own environment, and listens on that address for the length of the test.
With `control` in the file, it listens on that address instead and needs no `SCRIMMAGE_CONTROL_URL`.
If the address cannot be bound, the test stops with exit 3.

### Start it without `scrimmage-run`

A start line that cannot be changed, such as a manifest's `command`, can set the same folder on `PYTHONPATH` itself.
`scrimmage path` prints it, and a team reads it once from its own image:

```bash
docker run --rm acme/agent:test scrimmage path
```

```yaml
command: ["uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: PYTHONPATH
    value: /usr/local/lib/python3.12/site-packages/scrimmage_core/_boot   # first, then any of your own
  - name: SCRIMMAGE_SERVICE
    value: "1"
```

The path belongs to the image: it changes with the Python version and the install, so read it again after either changes.
A wrong or stale path, or a folder of your own put first with its own `sitecustomize.py`, starts the service with no gates.
The probe then gets no report, and the test stops with exit 3.

## The settings

In `scrimmage.toml`, `service` replaces `function` or `command`:

| Key | What it does | Default |
|---|---|---|
| `service` | The agent's address, as `http://host:port/path`. Each run is one `POST` there. | none |
| `body` | The run's body, sent as JSON. It must hold `{question}` in a text value. | none |
| `headers` | Headers added to each run's request. A value may hold `${NAME}` and `{session}`. | empty |
| `answer` | Where the answer is in the JSON reply, such as `"reply"`. Without it, the reply must be `text/plain` or a JSON string. | none |
| `record_grace_s` | How long a run's record stays open after its reply, for work the service does after it answers. 0 to 60. | 5 |
| `record_wait_s` | How much longer the runner waits for a run's last lines after the grace. Above 0, up to 120. | 10 |
| `background_real` | Calls that work outside any request may make for real while a test runs, such as a telemetry exporter. | empty |
| `parallel` | Runs at the same time, each its own request. | 1 |
| `control` | The `ip:port` the runner listens on, for a service in a container. | the address of `SCRIMMAGE_CONTROL_URL` |
| `control_tls` | `{cert = "path", key = "path"}`: the certificate and key the runner speaks TLS with. | none |

[Configuration](configuration.md#agent) has every key, and `real` and `tools` work as they do for a function.

## Placeholders

`${NAME}` in any text value of the file is read from the environment, so a token never sits in the file.
In `body` and `headers`, `{question}` and `{session}` are filled in for each run:

| Written | Sent |
|---|---|
| `"{question}"` in `body` | the test's question |
| `"{session}"` | `scrimmage-` and 16 random hex digits, new for every run |
| `"Bearer ${TEST_TOKEN}"` in `headers` | `Bearer` and the variable's value |
| `"{question}"` in `headers` | exit 2: a header cannot hold the question |

Every value read from the environment, of 8 characters or more, is removed from the records, the summary and `result.json`.
[Placeholders](configuration.md#placeholders) has the full rules.

## Before the first run

The runner checks the service twice before it sends a run.

**The probe.**
It sends one signed request, marked as a probe, to the service's address.
While nothing listens at that address, or a connection is taken and then reset or closed with no reply, it asks again for up to 30 seconds, so a script can start the service in the background and run the test at once.
If nothing listens by then, the test stops with exit 3 and says so.
Scrimmage in the service answers with a report signed with the same token: its version, its gates and the tools its gates swap.
The report's shape is the [startup report schema](schemas.md).
If no gated service answers, because a proxy dropped Scrimmage's header, the service was started without `scrimmage-run`, or without `PYTHONPATH` beginning with the folder `scrimmage path` prints, and `SCRIMMAGE_SERVICE=1`, its token is another, or its SDK speaks another protocol version, the test stops with exit 3 and sends nothing more.
The probe carries no protocol marker, since a service answers a stranger as it would without Scrimmage, so a failed probe cannot tell those causes apart.
If the service gates other tools than the file lists, the test stops with exit 2 and prints the two variables to start it with.

**The version rule.**
Before 1.0 the service and the runner run the same `scrimmage` version.
The report holds the version of `scrimmage` in the service, and the runner compares it with its own.
The same version passes, and nothing is said.
Any other version, even another patch or dev release, stops the test with exit 2 before any run, with one message naming both versions and the install line:

```text
the service runs scrimmage 0.4.1 and this runner is 0.4.0; install scrimmage 0.4.0 in the service
```

A version of `unknown`, from a `scrimmage` with no package metadata, never matches, not even another `unknown`.

Every claim of a run's plan carries the SDK's version too, so a process added later, such as a second worker from another image, is checked at its first run.
A claim from another version, or with no version in it, is refused with `503 version`, unlike the other gate codes it stops the test with exit 2, because every run from that process would fail the same way.
A function or a script whose child runs on another venv's SDK is not version-checked: see [limits](limits.md).

**The ping.**
Before it answers a probe, the service sends one signed request to the control server, through the same connection and certificate check as every other control request.
The control server answers with a random id it made for this test, and the runner checks the id is its own, so the ping proves this runner and not any runner that holds the token.
The report's `control` object holds `reached`, `tls`, `error`, `detail`, `rtt_ms`, `runner_id` and `url`.
`tls` is `verified`, `none` for `http://` on loopback, the class of the error when the ping failed over `https://`, or `not connected` when every control slot was busy and no ping was tried.
`error` is the class of the error that stopped the ping, such as `TimeoutError`, on `http://` and `https://` alike, and `null` when the ping was answered or was not tried.
A runner reads `error`, never `tls`, and an older service that sends no `error` is read as having none.

A new pod is not reachable at once: kube-proxy and the network plugin take a moment to route to it.
When the ping fails with a timeout, a refused connection or a name that does not resolve, the runner probes again every second for up to 30 seconds from its first probe, and prints once `scrimmage: waiting for the service to reach the control server at`, the address and what is missing, such as `(the name does not resolve yet)`.
Only the probe is sent again.
No setup request and no run is sent until a ping succeeds, and a run is never sent twice.
A certificate that does not verify, a reply from another runner and an error answered by the control server stop the test at once, as does a ping that was never made because every control slot was busy.

If the ping still fails after the wait, or reached another runner, the test stops with exit 3 before the setup request, sends no run and writes none to the records folder.
The text after `asking:` is the error's own message, which differs between systems; the rest is fixed.

| The ping found | The test stops with |
|---|---|
| A timeout, as when a NetworkPolicy or a firewall drops the traffic, or a Service has no ready endpoints under an egress policy that allows only named pods | `the service cannot reach the control server at https://scrimmage:7700 after 30 s of asking: timed out; nothing answered, as when a NetworkPolicy or a firewall drops the traffic, or when the control Service has no ready endpoints and an egress policy allows only named pods; allow the service's pods to reach this runner on port 7700, and check that the Service's selector matches this runner's pod` |
| A refused connection, as when a Kubernetes Service has no ready endpoints | `the service cannot reach the control server at https://scrimmage:7700 after 30 s of asking: [Errno 111] Connection refused; nothing listens there, as when a Kubernetes Service has no ready endpoints; check that the Service's selector matches this runner's pod` |
| A name that does not resolve | `the service cannot reach the control server at https://scrimmage:7700 after 30 s of asking: [Errno -2] Name or service not known; the name scrimmage does not resolve where the service runs; check the host in SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace` |
| A certificate from another authority | `the service cannot reach the control server at https://scrimmage:7700: ... certificate verify failed ...; check SCRIMMAGE_CONTROL_CA`, at the first probe |

**The clock.**
Every signed request carries the sender's time, and a request more than 60 seconds from the receiver's clock is refused.
Scrimmage does not measure or correct a gap: each side signs with its own clock.
A gap under 60 seconds changes nothing and is not reported.
Past 60 seconds the service's gate cannot take the probe as one, so it answers a signed `409` that holds no time, and the test stops with exit 3 before the setup request:

```text
the service's clock and this machine's differ by more than 60 s, so every signed request is refused; set both from the same NTP source
```

The service's ping to the control server is a signed request too, so a gap there stops the test with the same message.
NTP, the Network Time Protocol, is what servers use to keep their clocks in step; set the runner's machine and the service's from the same source.

**The setup request.**
Then it sends the first test's baseline once, as a run named `setup-` and 12 hex digits.
Its record is kept beside the runs, and its model calls count toward the budget, but no row counts it.
It must be claimed by Scrimmage in the service, answer 2xx with an answer where `answer` says, refuse nothing, make its model call inside its run, and see no call outside any request.
A refusal names the entry to add, such as `"process:uname"` under `real`, and stops the test with exit 2.
A reply with no model call inside the run stops it with exit 3: the run's context is lost on the agent's main path, so every run would be an infrastructure error.

## Each run

Each run is one `POST` with `body` and `headers`, plus one header, `x-scrimmage-run`, that names the run.
The service's gate checks the header, claims the run's plan from the runner, and removes every `x-scrimmage-` header before your app sees the request.
Everything the request does, on its loop, its executor's threads and the tasks and threads it starts, follows that run's plan.
A request with no such header reaches the app untouched, byte for byte, and its calls run as they would without Scrimmage.

A run's record stays open `record_grace_s` after the reply's last byte, so a background task that runs after the reply is in it.
The runner then waits up to `record_wait_s` more for the last lines.
A record whose last lines never came is incomplete, and the run is an infrastructure error.

Each run's last batch also carries a digest of the plan its process used, and the runner compares it with the digest of its own plan.
A run served under another plan is an infrastructure error, and its summary names the process that claimed it in `claimed_by`: [`plan_digest`, `claimed_plan_digest` and `claimed_by`](json-output.md#a-service-runs-summary-line).
A claimed run's last batch with no digest is refused as a bad batch, so its record is incomplete and the run is an infrastructure error.

A streamed reply, such as `text/event-stream`, stops the test with exit 2: this version judges one JSON or text reply per run.

## When the gate answers 503

When the gate cannot run a request under its plan, it answers `503` itself, with the code in an `x-scrimmage-error` header and in a JSON body, `{"error": "<code>"}`, and your app never sees the request:

| Code | Why |
|---|---|
| `bad-run-header` | The header is malformed, appears twice, or carries a MAC made without the token. No control request is made. |
| `no-such-run` | The runner has no such run open: it ended, or never was. |
| `claimed` | Another request claimed this run already, such as a replayed header. |
| `clock` | The control server refused the claim because the service's clock and the runner's differ by more than 60 seconds, as after a clock jump during the test. The run's message says to set both clocks from the same NTP source. |
| `plan-unavailable` | No control slot was free in about 250 ms, the runner could not be reached, or the plan names a tool or a package the service does not gate. |
| `structure` | The process found a library it cannot gate after it started listening. Plain requests are still served. |

The runner reads each of these as an infrastructure error for that run, exit 3, and the test goes on.
A reply that only looks like the gate's, or a run whose plan was never claimed, stops the test with exit 3: a process without Scrimmage answered it.

## In Docker Compose

A service in a container cannot reach a runner on `127.0.0.1`, so the runner listens on an address the container can reach, and the control channel runs over TLS.
[A service in Docker Compose](docker.md) has the settings, the Compose file, the commands and what crosses the network.

## In a Kubernetes cluster

In a cluster, the runner runs as a Job and the service as a Deployment, with the token and the control key as files from Secret volumes.
[A service in Kubernetes](kubernetes.md) has the Secret files, every manifest, the commands and how to read the result.

## Workers

`uvicorn --workers N` works as it does without Scrimmage: each worker loads the app, prints its own active line, and serves runs and plain requests.
The supervisor loads no app and says nothing.
gunicorn's workers, `UvicornWorker` included, are forks of one process and have none of Scrimmage's threads, so each stops at its first request and says so.
Which worker takes a run is up to the kernel, and a run's lines always carry the process that served it.

Each process sends at most 8 control requests at once, so 8 processes fill the control server's 64 connections.
A connection past those waits until one frees, or for up to 10 seconds, and then its run is an infrastructure error.
Each run's summary says how busy its process was, as [`control_busy` and `control_p99_ms`](json-output.md): both describe the process, not the run.
A `control_p99_ms` rising toward 80 ms is the warning before runs end with `plan-unavailable`.

## Work outside any request

Code that runs outside any request, such as a startup job, a lazy worker thread or a thread a plain request left running, cannot be told from a run's own while a run is open.
While a run is open, such code follows the outside rules: a read or a model call runs, and a write is refused unless `background_real` lists it.
Either way every open run is told, and ends as an infrastructure error, since the call could have been its own.
A call `background_real` lets through is told too, and ends nothing.
The setup request lists each such call it saw with the entry to add.
See [Limits](limits.md) for the cost of this rule.

## What is not covered

- `https` service addresses, a certificate rotated while a test runs, a proxy that ends TLS between the service and the runner, and a second runner against the same service at once.
- A streamed reply, a WebSocket, several turns per run, and a service that answers 202 and finishes elsewhere.
- WSGI services, Flask, Django and gunicorn, queue workers, and hypercorn.
- `scrimmage record` and `scrimmage init --probe`, which stop with exit 2: record or probe the same agent as a function.
- A Python child joining a service run: every process start in a run is refused unless `real` lists its program. See [Safety](safety.md#an-agent-inside-a-web-service).
- An MCP server over stdio started inside a request: its start is a process start, so it needs `process:<command>` under `real`, such as `process:npx`. A server started at the service's startup needs nothing; its tool calls are gated either way.

[Limits](limits.md) lists the rest.
