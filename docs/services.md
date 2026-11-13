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
| `SCRIMMAGE_TOKEN_FILE` | The path of the token file: a plain file you own, mode `600`, with a token of 32 characters or more. Any other mode stops the service. |
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
While nothing listens at that address, it asks again for up to 30 seconds, so a script can start the service in the background and run the test at once.
If nothing listens by then, the test stops with exit 3 and says so.
Scrimmage in the service answers with a report signed with the same token: its versions, its gates and the tools its gates swap.
The report's shape is the [startup report schema](schemas.md).
If no gated service answers, because a proxy dropped Scrimmage's header, the service was started without `scrimmage-run` and `SCRIMMAGE_SERVICE=1`, its token is another, or its SDK speaks another protocol version, the test stops with exit 3 and sends nothing more.
The probe carries no protocol marker, since a service answers a stranger as it would without Scrimmage, so a failed probe cannot tell those causes apart.
If the service gates other tools than the file lists, the test stops with exit 2 and prints the two variables to start it with.

**The version rule.**
The report holds the version of `scrimmage-core` in the service, and the runner compares it with its own.
The major is the first number of the version, or, while it is 0, the first two, since a 0.x release may change anything.

| The service's SDK against the runner | What happens |
|---|---|
| The same release | nothing |
| The same major, another minor, patch, pre-release or local part | one warning, in the report, naming both versions. The test goes on |
| Another major | exit 2 before any run, naming both versions and the install line |
| A version that is not an installed `scrimmage-core` release, such as `unknown` | exit 2 |
| A startup report older than this runner reads (version 1) | exit 2: install the runner's version in the service |

Every claim of a run's plan carries the SDK's version too, so a process added later, such as a second worker from another image, is checked at its first run.
A claim from another major, or with no version in it, is refused with `503 version`, unlike the other gate codes it stops the test with exit 2, because every run from that process would fail the same way.
A function or a script whose child runs on another venv's SDK is not version-checked: see [limits](limits.md).

**The ping.**
Before it answers a probe, the service sends one signed request to the control server, through the same connection and certificate check as every other control request.
The control server answers with a random id it made for this test, and the runner checks the id is its own, so the ping proves this runner and not any runner that holds the token.
The report's `control` object holds `reached`, `tls`, `detail`, `rtt_ms`, `runner_id` and `url`; `tls` is `verified`, `none` for `http://` on loopback, the class of the error when the ping failed over `https://`, or `not connected` when every control slot was busy and no ping was tried.
If the service cannot reach the control server, or reached another runner, the test stops with exit 3 before the setup request, and names the error, such as a certificate that did not verify.

**The clock.**
The runner works out how far the service's clock is from its own, from the service's time in the report and the middle of the probe's round trip.
`scrimmage check --service` always prints the gap.
`scrimmage test` says it as a warning from 10 seconds, and stops with exit 3 before the setup request from 50 seconds, because a request more than 60 seconds off is refused and a clock that is still moving would soon pass that.
Past 60 seconds the service's gate cannot take the probe as one, so it answers a signed `409` with its time, and the test stops with exit 3 naming the gap.
The service's calls to the control server are not affected: its client corrects its own clock from the control server's signed replies.
Set both clocks from the same time source (NTP) to avoid all of this.

**The template.**
Before the setup request, and in `scrimmage check --service`, the runner sends one plain `GET` for `/openapi.json` to the service, with the file's `headers` expanded and no body, and checks the file's `body` against the document.
It is a request from the runner's own process, so no gate sees it and the app gets it untouched, with no `x-scrimmage-` header.
It asks at the service's origin first, then under each prefix of the service's path, longest first, at most three requests in all, so a service at `/api/chat` mounted under `/api` is found at `/api/openapi.json`.
It renders `body` with the question `x` and a new session, and checks a small part of JSON Schema: that every `required` name is present, that no field is outside `properties` when `additionalProperties` is `false`, and each present field's JSON type, one level down for an object.
It follows `$ref` only inside `components/schemas`, and looks through pydantic's `anyOf` with `null` for an optional field.
Each misfit is one warning, such as `[agent] body: /openapi.json says POST /chat requires "message"; the template has no such field`.
A warning never stops `scrimmage test`; `scrimmage check --service --strict` exits 2 on one.
No document, a document that is not JSON or too deeply nested to read, or no entry for the path is one note.
Not checked: `oneOf`, `pattern`, `format`, `items`, and anything more than one level down; the note says so.
A pydantic model with `populate_by_name` accepts a field under its Python name as well as its alias, but the document lists only the alias.
A file that sends the Python name is warned about, for a missing alias under `required` and, with `additionalProperties` false, for a field the document does not allow.
The warning is false in that case: it never stops a test, and `--strict` is the only place it matters, so leave `--strict` off for such a service.

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

A streamed reply, such as `text/event-stream`, stops the test with exit 2: this version judges one JSON or text reply per run.

## When the gate answers 503

When the gate cannot run a request under its plan, it answers `503` itself, with the code in an `x-scrimmage-error` header and in a JSON body, `{"error": "<code>"}`, and your app never sees the request:

| Code | Why |
|---|---|
| `bad-run-header` | The header is malformed, appears twice, or carries a MAC made without the token. No control request is made. |
| `no-such-run` | The runner has no such run open: it ended, or never was. |
| `claimed` | Another request claimed this run already, such as a replayed header. |
| `plan-unavailable` | No control slot was free in about 250 ms, the runner could not be reached, or the plan names a tool or a package the service does not gate. |
| `structure` | The process found a library it cannot gate after it started listening. Plain requests are still served. |

The runner reads each of these as an infrastructure error for that run, exit 3, and the test goes on.
A reply that only looks like the gate's, or a run whose plan was never claimed, stops the test with exit 3: a process without Scrimmage answered it.

## A service in a container

A container has its own loopback, so a service in one cannot reach a runner on `127.0.0.1`.
For that case the runner listens on an address of its host that the container can reach, and the control channel runs over TLS.

1. Make the certificates: `scrimmage cert --ip 172.17.0.1 --name host.docker.internal --out .scrimmage-cert`. See [`scrimmage cert`](cli.md#scrimmage-cert).
2. In `scrimmage.toml`, name where the runner listens and what it speaks:

    ```toml
    [agent]
    service     = "http://127.0.0.1:8765/chat"
    body        = {message = "{question}"}
    control     = "172.17.0.1:8766"
    control_tls = {cert = ".scrimmage-cert/server.pem", key = ".scrimmage-cert/server.key"}
    ```

3. Start the service with `SCRIMMAGE_CONTROL_URL=https://host.docker.internal:8766` and `SCRIMMAGE_CONTROL_CA` set to the path of `ca.pem` inside the container.
   The token file is a plain file, mode `600`, owned by the process that reads it.
   Run the container as the file's owner, such as with `user: "1000:1000"` in Docker Compose, or the service stops and names both user ids.

### With Docker Compose

This repository tests the example service this way, and a service of yours can follow it.
Compose is used for test environments here.
The file below runs the service in one container, with two workers, and the runner on the host.

```yaml title="compose.yaml"
# The acme order bot as a service, with the runner on the host. See the services page of the docs.
# Set before `docker compose up`:
#   SCRIMMAGE_UID, SCRIMMAGE_GID   the token file owner's ids, from `id -u` and `id -g`
#   SCRIMMAGE_TOKEN_FILE           the token file, mode 600
#   SCRIMMAGE_CA_FILE              ca.pem from `scrimmage cert`
#   SCRIMMAGE_CONTROL_PORT         the port of `control` in scrimmage.toml
services:
  agent:
    image: ${SCRIMMAGE_IMAGE:-scrimmage-acme-service}
    # The token file must be read by its owner, so the container runs as that user.
    user: "${SCRIMMAGE_UID:?set SCRIMMAGE_UID to the token file owner's uid, $(id -u)}:${SCRIMMAGE_GID:?set SCRIMMAGE_GID to $(id -g)}"
    ports:
      - "127.0.0.1::8765"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      SCRIMMAGE_SERVICE: "1"
      SCRIMMAGE_TOKEN_FILE: /run/scrimmage/token
      SCRIMMAGE_CONTROL_URL: https://host.docker.internal:${SCRIMMAGE_CONTROL_PORT:?set SCRIMMAGE_CONTROL_PORT to the port of control in scrimmage.toml}
      SCRIMMAGE_CONTROL_CA: /run/scrimmage/ca.pem
      SCRIMMAGE_TOOLS: acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock
      SCRIMMAGE_ALLOW_IMPORTS: acme.
    volumes:
      - scratch:/tmp  # the one place the app may write
      - type: bind
        source: ${SCRIMMAGE_TOKEN_FILE:?set SCRIMMAGE_TOKEN_FILE}
        target: /run/scrimmage/token
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ${SCRIMMAGE_CA_FILE:?set SCRIMMAGE_CA_FILE}
        target: /run/scrimmage/ca.pem
        read_only: true
        bind:
          create_host_path: false
    command: ["scrimmage-run", "uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "2"]

volumes:
  scratch: {}
```

Four things in it matter:

- `user:` runs the container as the token file's owner.
  Compose refuses to start when `SCRIMMAGE_UID` or `SCRIMMAGE_GID` is unset, because bash does not export `UID`, so Scrimmage's own two variables carry the ids.
- The token file and `ca.pem` are read-only bind mounts, and `create_host_path: false` makes a missing file an error, not a folder Docker made.
  Neither file is in the image.
- `SCRIMMAGE_TOKEN_FILE` and `SCRIMMAGE_CONTROL_CA` name the paths inside the container, never the host's.
- `host.docker.internal:host-gateway` is Docker's own name for the host's bridge address, `172.17.0.1` on a default install.
  `docker network inspect bridge` shows yours; give that address to `scrimmage cert --ip` and to `control`.

The image holds the app and the Scrimmage SDK and nothing else of yours.
Build it from a folder that holds only a Dockerfile, the wheels and the app, with `docker build --network none`, so that nothing is fetched at build time and no `.env`, key or token file can enter the image.

Then, from the folder of your `scrimmage.toml`:

```bash
scrimmage cert --ip 172.17.0.1 --name host.docker.internal --out .scrimmage-cert
export SCRIMMAGE_UID=$(id -u) SCRIMMAGE_GID=$(id -g) SCRIMMAGE_CONTROL_PORT=8766
export SCRIMMAGE_TOKEN_FILE=$PWD/.scrimmage-token SCRIMMAGE_CA_FILE=$PWD/.scrimmage-cert/ca.pem
docker compose up -d
docker compose port agent 8765        # the host port Docker chose, such as 127.0.0.1:49153
scrimmage check scrimmage.toml --service
scrimmage test scrimmage.toml
docker compose down -v
```

Put the host port from `docker compose port` in `service`, and the same `SCRIMMAGE_CONTROL_PORT` in `control`.
`scrimmage check --service` asks the service to ping the runner over TLS, so a wrong authority, a wrong address or a wrong user shows there, before any run.
The runner refuses `control = "0.0.0.0:8766"`, an address that listens on every interface: it must be one address of the host.

The SDK trusts the one authority in `SCRIMMAGE_CONTROL_CA` and nothing else; it never loads the system's certificate store.
It speaks TLS 1.3 only, checks the host name or address in `SCRIMMAGE_CONTROL_URL` against the certificate, and applies strict chain rules.
The authority's key is discarded once the server certificate is signed, and the authority is limited to the names and addresses you gave and their subdomains.
The HMAC signing stays on every request and reply, under TLS.

What crosses the network, exactly:

| From | To | What | Protected by |
|---|---|---|---|
| The service | The runner, at `SCRIMMAGE_CONTROL_URL` | Claims, plans, record lines, side-folder items and budget asks | TLS 1.3 with your authority, and the HMAC on every request and reply |
| The runner | The service, at `service` | The runs: request bodies, the `headers` from the file, an `Authorization` header included, and the whole answer | The run header's MAC proves who sent it. Nothing protects the content: it crosses the container network in clear |
| The service | Anywhere else | The agent's own calls | The gates, as without a container |

The runner's requests to the service stay `http://` in this version.
A container with Docker's default `CAP_NET_RAW` on the same bridge can read them.
Keep a real credential out of `headers` when the service runs in a container.

Who can reach the runner's address: every container on every Docker network on the host, every Kubernetes node and pod, and a peer on the host's network that routes the address to it.
The runner keeps no list of who may connect, so a client without the token can still open connections.
It can read nothing and send nothing the signature accepts, and it can hold the control server's 64 connections.
Each connection is cut after 10 seconds without a read and 15 seconds in all, handshake included, and a request that declares a body over 4 MB is read only if its signature is good.
While every connection is held, runs end as `plan-unavailable` infrastructure errors, never as results.
The report's `control server:` lines count what the runner saw, so this shows even when no run was claimed:

```text
control server: runs sent 5, claims received 0
control server: 64 connections cut by the timeouts, a client that stalled or dripped
control server: 3 connections failed TLS (unknown ca 2, wrong version number 1)
```

`claims received` far below `runs sent` means no service reached the control server: a wrong address, a wrong authority, or a held port.
Each control request opens a new connection and a TLS handshake, so TLS adds about 1.7 ms to a request's median, and `control_p99_ms` rises sooner than without it.
Keeping connections alive is a later step.

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
