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
| `SCRIMMAGE_TOKEN_FILE` | The path of the token file: a plain file you own, mode `600`, or a Kubernetes Secret volume file (see [the token in a pod](#the-token-and-the-key-in-a-kubernetes-pod)), with a token of 32 characters or more. Any other mode stops the service. |
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
Scrimmage in the service answers with a report signed with the same token: its versions, its gates and the tools its gates swap.
The report's shape is the [startup report schema](schemas.md).
If no gated service answers, because a proxy dropped Scrimmage's header, the service was started without `scrimmage-run`, or without `PYTHONPATH` beginning with the folder `scrimmage path` prints, and `SCRIMMAGE_SERVICE=1`, its token is another, or its SDK speaks another protocol version, the test stops with exit 3 and sends nothing more.
The probe carries no protocol marker, since a service answers a stranger as it would without Scrimmage, so a failed probe cannot tell those causes apart.
If the service gates other tools than the file lists, the test stops with exit 2 and prints the two variables to start it with.

**The version rule.**
The report holds the version of `scrimmage` in the service, and the runner compares it with its own.
The major is the first number of the version, or, while it is 0, the first two, since a 0.x release may change anything.

| The service's SDK against the runner | What happens |
|---|---|
| The same release | nothing |
| The same major, another minor, patch, pre-release or local part | one warning, in the report, naming both versions. The test goes on |
| Another major | exit 2 before any run, naming both versions and the install line |
| A version that is not an installed `scrimmage` release, such as `unknown` | exit 2 |
| A startup report older than this runner reads (version 1) | exit 2: install the runner's version in the service |

Every claim of a run's plan carries the SDK's version too, so a process added later, such as a second worker from another image, is checked at its first run.
A claim from another major, or with no version in it, is refused with `503 version`, unlike the other gate codes it stops the test with exit 2, because every run from that process would fail the same way.
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

Each run's last batch also carries a digest of the plan its process used, and the runner compares it with the digest of its own plan.
A run served under another plan is an infrastructure error, and its summary names the process that claimed it in `claimed_by`: [`plan_digest`, `claimed_plan_digest` and `claimed_by`](json-output.md#a-service-runs-summary-line).
A service whose `scrimmage` is older does not send the digest: nothing is compared, and the report says to upgrade it.

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
    # The image's files are read-only to the app; /tmp is the one place it may write.
    read_only: true
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
      - scratch:/tmp
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

Five things in it matter:

- `user:` runs the container as the token file's owner.
  Compose refuses to start when `SCRIMMAGE_UID` or `SCRIMMAGE_GID` is unset, because bash does not export `UID`, so Scrimmage's own two variables carry the ids.
- `read_only: true` makes the image's files read-only to the app, and `/tmp` is a volume it may write.
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

Run `scrimmage check --service` or `scrimmage test` right after `up -d`; no wait is needed.
Docker's published port takes a connection before the app inside listens and resets or closes it, and the runner asks again for up to 30 seconds.
A wrong address that accepts a connection and then closes it costs the full 30 second wait before the test stops with exit 3.
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

## The token and the key in a Kubernetes pod

In a pod, give the token and the control key to Scrimmage as files from a Secret volume, never as `env` with `secretKeyRef`.
An environment variable is inherited by every program the agent starts, and `kubectl exec <pod> -- env` shows it.
A service with `SCRIMMAGE_TOKEN` in its environment stops, and the runner reads `SCRIMMAGE_TOKEN_FILE` only.

```yaml
securityContext:
  runAsUser: 10001
  runAsNonRoot: true
  fsGroup: 10001
containers:
  - name: agent
    env:
      - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
    volumeMounts:
      - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
volumes:
  - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
```

The kubelet writes a Secret file as root, with the pod's `fsGroup` as its group and mode `440`, and `token` is a link to `..data/token`.
Scrimmage still reads a plain file you own with mode `600` first.
When the path is a Secret volume's link, it reads the file only if all of these hold:

- the path is the kubelet's own link, `token -> ..data/token`, and `..data` is a link to a folder of the same directory; a link to anywhere else is refused;
- the process is not root;
- root owns the file;
- the file's group is one of the process's groups, which `fsGroup` adds;
- the mode has no bit for others, none to write and none to run: `400` or `440`;
- the volume is mounted read-only.

Each refusal names the setting that fixes it: `securityContext.fsGroup`, `runAsUser` and `runAsNonRoot: true`, or the volume's `defaultMode`.
A plain file owned by another user, and a plain file of mode `640`, are still refused.

## In Kubernetes

In a cluster, the runner runs as a Job, and the service runs as a Deployment of two replicas behind a Service.
Every secret arrives as a Secret volume.
This repository tests the example service this way on kind, with the files below.
kind is the Kubernetes project's own test cluster; the files are the same on a production cluster.

1. One namespace holds the test, and enforces the Pod Security `restricted` standard.
2. cert-manager issues the control server's certificate.
3. A Secret, `scrimmage-token`, holds the token.
4. The agent runs as the Deployment `acme-agent`, two replicas of `uvicorn --workers 2`, behind the Service `acme-agent` on port 8000.
5. The runner runs as the Job `scrimmage-test`, behind the Service `scrimmage` on port 7700.
   It writes its records to a volume claim, `scrimmage-records`.
6. NetworkPolicies deny everything in the namespace, then allow the agent to reach the runner on 7700, the runner to reach the agent on 8000, the agent to reach the model, and every pod to reach cluster DNS.
7. The agent's pods reach the runner at `https://scrimmage:7700`, and the runner reaches the agent at `http://acme-agent:8000/chat`.

The cluster needs three things:

- a network plugin that enforces NetworkPolicy, such as Calico, which the tests use;
- cert-manager, the CNCF project most clusters issue certificates with;
- a storage class for the records: the tests use `standard`, kind's local-path class, which is for tests only, and a production cluster names a CSI class, such as the EBS one.

`TAG` in an image name stands for your image's tag, and `NAMESPACE` for the test's namespace.
`imagePullPolicy: Never` runs only an image already on the node; use your registry's policy instead.

### The namespace and the accounts

```yaml title="namespace.yaml"
# The namespace one test runs in. The harness replaces NAMESPACE with s8-<random>.
# enforce refuses a pod that breaks the restricted standard. A Deployment or a Job that breaks it
# is still admitted and only its pods are refused, so warn prints the problem when the workload
# is applied, and audit records it. The version is the cluster's own minor, pinned.
apiVersion: v1
kind: Namespace
metadata:
  name: NAMESPACE
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.36
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: v1.36
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: v1.36
```

None of the accounts has a Role, and no pod gets an API token: the runner never calls the Kubernetes API, and a pod needs no rights to mount a Secret.

```yaml title="service-accounts.yaml"
# No Role and no RoleBinding for any of them, and no API token mounted into a pod.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: scrimmage-runner
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: acme-agent
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fake-model
automountServiceAccountToken: false
```

### The certificate

The authority and its Issuer live in a namespace of their own, `scrimmage-pki`, made once per cluster.
cert-manager signs every request an Issuer gets, so an Issuer in the test's namespace would sign for the control name for anyone who can make a `Certificate` there.

```yaml title="pki-authority.yaml"
# The certificate authority for the control channel, made once per cluster in scrimmage-pki.
# No test namespace holds an Issuer.
apiVersion: v1
kind: Namespace
metadata:
  name: scrimmage-pki
---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: scrimmage-selfsigned
  namespace: scrimmage-pki
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: scrimmage-ca
  namespace: scrimmage-pki
spec:
  isCA: true
  commonName: scrimmage-ca
  secretName: scrimmage-ca
  duration: 43800h
  # A DNS constraint permits a name and every name that ends with it. scrimmage is the host in
  # SCRIMMAGE_CONTROL_URL, svc covers scrimmage.<namespace>.svc and cluster.local covers
  # scrimmage.<namespace>.svc.cluster.local. Every IP address is excluded. A client that checks
  # the constraint refuses any other name or any address this authority signs.
  nameConstraints:
    critical: true
    permitted:
      dnsDomains:
        - scrimmage
        - svc
        - cluster.local
    excluded:
      ipRanges:
        - 0.0.0.0/0
        - "::/0"
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Never
  issuerRef:
    name: scrimmage-selfsigned
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: scrimmage-ca
  namespace: scrimmage-pki
spec:
  ca:
    secretName: scrimmage-ca
```

One certificate per test namespace, with `NAMESPACE` filled in:

```yaml title="pki-control.yaml"
# The control server's certificate for one test namespace. The harness fills in NAMESPACE.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: scrimmage-control-NAMESPACE
  namespace: scrimmage-pki
spec:
  secretName: scrimmage-control-NAMESPACE
  duration: 24h
  privateKey:
    algorithm: ECDSA
    size: 256
  usages:
    - server auth
    - digital signature
  dnsNames:
    - scrimmage
    - scrimmage.NAMESPACE.svc
    - scrimmage.NAMESPACE.svc.cluster.local
  issuerRef:
    name: scrimmage-ca
    kind: Issuer
```

Copy its Secret into the test's namespace as two Secrets: `scrimmage-control-tls` with `tls.crt` and `tls.key` for the runner, and `scrimmage-ca-cert` with only `ca.crt` for the agent.
No key is ever in the agent's pod.

```bash
NS=my-test
SOURCE="secret/scrimmage-control-$NS -n scrimmage-pki"
kubectl create secret generic scrimmage-control-tls -n $NS \
  --from-file=tls.crt=<(kubectl get $SOURCE -o jsonpath='{.data.tls\.crt}' | base64 -d) \
  --from-file=tls.key=<(kubectl get $SOURCE -o jsonpath='{.data.tls\.key}' | base64 -d)
kubectl create secret generic scrimmage-ca-cert -n $NS \
  --from-file=ca.crt=<(kubectl get $SOURCE -o jsonpath='{.data.ca\.crt}' | base64 -d)
```

The key passes through a pipe, never a file.
The authority lives 5 years and keeps its key, so a renewed server certificate never changes what the agent trusts.

### The token

Make the token Secret from standard input, so the token is on no command line and in no file:

```bash
python3 -c "import secrets; print(secrets.token_hex(32), end='')" \
  | kubectl create secret generic scrimmage-token -n $NS --from-file=token=/dev/stdin
```

### The records volume, the Services and the policies

The records outlive the Job's pod on the claim, and are read back by the reader pod below.
Records hold your test's data: delete the claim when you no longer need them.

```yaml title="records-pvc.yaml"
# The class "standard" is kind's local-path provisioner. A production cluster names a CSI class.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: scrimmage-records
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 1Gi
```

```yaml title="services.yaml"
apiVersion: v1
kind: Service
metadata:
  name: scrimmage
spec:
  selector:
    app.kubernetes.io/name: scrimmage-runner
  ports:
    - {name: control, port: 7700, targetPort: control}
---
apiVersion: v1
kind: Service
metadata:
  name: acme-agent
spec:
  selector:
    app.kubernetes.io/name: acme-agent
  ports:
    - {name: http, port: 8000, targetPort: http}
---
apiVersion: v1
kind: Service
metadata:
  name: fake-model
spec:
  selector:
    app.kubernetes.io/name: fake-model
  ports:
    - {name: http, port: 8080, targetPort: http}
```

```yaml title="network-policies.yaml"
# Deny everything, then allow four paths: the agent to the runner on 7700, the runner to the
# agent on 8000, the agent to the fake model on 8080, and every pod to cluster DNS.
# A policy selects one set of pods, so each path is two policies: egress on the sender, ingress
# on the receiver. Calico checks a policy after a Service's address is translated, so the rules
# name pods and ports, never Service addresses.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dns
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: kube-system}
          podSelector:
            matchLabels: {k8s-app: kube-dns}
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-runner-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: scrimmage-runner}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 7700}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-runner-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: scrimmage-runner}
      ports:
        - {protocol: TCP, port: 7700}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runner-to-agent-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: scrimmage-runner}
      ports:
        - {protocol: TCP, port: 8000}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runner-to-agent-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: scrimmage-runner}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 8000}
---
# The real-model check replaces the two agent-to-model policies with TCP 443 to 0.0.0.0/0,
# except the pod and service ranges, 172.16.0.0/12 and 169.254.0.0/16, since a NetworkPolicy
# cannot name a host.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-model-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: fake-model}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 8080}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-model-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: fake-model}
      ports:
        - {protocol: TCP, port: 8080}
```

The tests run a fake model in the namespace, the `fake-model` the policies name.
With a real provider, replace the two agent-to-model policies as their comment says.

### The agent

```yaml title="agent.yaml"
# The acme order bot as a service: two replicas, each uvicorn with two workers.
# The token and the authority arrive as Secret volumes. The service refuses a token in its
# environment. The authority's Secret holds ca.crt alone, so no key is ever in this pod.
# OPENAI_API_KEY is the fake model's, not a secret.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-agent
spec:
  replicas: 2
  selector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  template:
    metadata:
      labels: {app.kubernetes.io/name: acme-agent}
    spec:
      serviceAccountName: acme-agent
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: agent
          image: scrimmage-compose-service:TAG
          imagePullPolicy: Never
          command: ["scrimmage-run", "uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
          env:
            - {name: SCRIMMAGE_SERVICE, value: "1"}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
            - {name: SCRIMMAGE_CONTROL_URL, value: "https://scrimmage:7700"}
            - {name: SCRIMMAGE_CONTROL_CA, value: /run/scrimmage/ca/ca.pem}
            - {name: SCRIMMAGE_TOOLS, value: "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"}
            - {name: SCRIMMAGE_ALLOW_IMPORTS, value: "acme."}
            - {name: OPENAI_BASE_URL, value: "http://fake-model:8080/v1"}
            - {name: OPENAI_API_KEY, value: test}
          ports: [{containerPort: 8000, name: http}]
          readinessProbe:
            httpGet: {path: /openapi.json, port: http}
            periodSeconds: 2
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: ca, mountPath: /run/scrimmage/ca, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: ca
          secret:
            secretName: scrimmage-ca-cert
            defaultMode: 0444
            items: [{key: ca.crt, path: ca.pem}]
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

### The agent without `scrimmage-run`

A Deployment that keeps its own `command` sets `PYTHONPATH` instead, as in [Start it without `scrimmage-run`](#start-it-without-scrimmage-run).
This file is `agent.yaml` with that one change, and the tests run the same test against it.
Read the path from your own image with `docker run --rm <image> scrimmage path`.

```yaml title="agent-pythonpath.yaml"
# The agent of agent.yaml, started without scrimmage-run, for a Deployment that keeps its own
# command. PYTHONPATH begins with the folder that holds Scrimmage's sitecustomize.py, and
# SCRIMMAGE_SERVICE=1 switches the service on, which is all scrimmage-run does for a service.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-agent
spec:
  replicas: 2
  selector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  template:
    metadata:
      labels: {app.kubernetes.io/name: acme-agent}
    spec:
      serviceAccountName: acme-agent
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: agent
          image: scrimmage-compose-service:TAG
          imagePullPolicy: Never
          command: ["uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
          env:
            # First on the path: the folder `scrimmage path` prints in this image. Read it once with
            # docker run --rm <image> scrimmage path
            - {name: PYTHONPATH, value: /usr/local/lib/python3.12/site-packages/scrimmage_core/_boot}
            - {name: SCRIMMAGE_SERVICE, value: "1"}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
            - {name: SCRIMMAGE_CONTROL_URL, value: "https://scrimmage:7700"}
            - {name: SCRIMMAGE_CONTROL_CA, value: /run/scrimmage/ca/ca.pem}
            - {name: SCRIMMAGE_TOOLS, value: "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"}
            - {name: SCRIMMAGE_ALLOW_IMPORTS, value: "acme."}
            - {name: OPENAI_BASE_URL, value: "http://fake-model:8080/v1"}
            - {name: OPENAI_API_KEY, value: test}
          ports: [{containerPort: 8000, name: http}]
          readinessProbe:
            httpGet: {path: /openapi.json, port: http}
            periodSeconds: 2
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: ca, mountPath: /run/scrimmage/ca, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: ca
          secret:
            secretName: scrimmage-ca-cert
            defaultMode: 0444
            items: [{key: ca.crt, path: ca.pem}]
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

### The Job and its file

The Job's file is the ConfigMap `scrimmage-config`.
`control` is the runner pod's own address, from the downward API, and the Service `scrimmage` points at it.
The pod's address is removed from the records, as is any `${NAME}` value of 8 characters or more.

```toml title="scrimmage.toml"
# The Job's file, the ConfigMap scrimmage-config. The tests below it are the example's.
# POD_IP is the runner pod's own address from the downward API, so the agent's pods reach the
# control server there. The Service `scrimmage` and the certificate's names point at it.
[agent]
service     = "http://acme-agent:8000/chat"
body        = {message = "{question}", session = "{session}"}
answer      = "reply"
tools       = ["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:refund_order", "acme.tools:get_stock"]
real        = ["tool:check_inventory", "process:uname"]
model_hosts = [{host = "fake-model", provider = "openai"}]
control     = "${POD_IP}:7700"
control_tls = {cert = "/run/scrimmage/tls/tls.crt", key = "/run/scrimmage/tls/tls.key"}
runs = 30
record_grace_s = 0.2
record_wait_s = 10
run_timeout = 30

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"             # returns an acme.models.Stock object
returns = {sku = "SKU-42", count = 12}

[[test.fault]]
call = "tool:get_order"
do   = "timeout"
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30", "October 30"]
```

`--records` puts the records folder inside the volume, not beside the read-only file.

```yaml title="runner-job.yaml"
# One test, one pod. backoffLimit 0: a retry would start a second test and spend twice.
# The pod's exit code is scrimmage test's: 0, 2 or 3. The result is result.json on the volume.
apiVersion: batch/v1
kind: Job
metadata:
  name: scrimmage-test
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    metadata:
      labels: {app.kubernetes.io/name: scrimmage-runner}
    spec:
      restartPolicy: Never
      serviceAccountName: scrimmage-runner
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: runner
          image: scrimmage-runner:TAG
          imagePullPolicy: Never
          args: ["test", "/etc/scrimmage/scrimmage.toml", "--records", "/records/scrimmage-runs"]
          env:
            - name: POD_IP
              valueFrom: {fieldRef: {fieldPath: status.podIP}}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
          ports: [{containerPort: 7700, name: control}]
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: config, mountPath: /etc/scrimmage, readOnly: true}
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: tls, mountPath: /run/scrimmage/tls, readOnly: true}
            - {name: records, mountPath: /records}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: config, configMap: {name: scrimmage-config}}
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: tls
          secret:
            secretName: scrimmage-control-tls
            defaultMode: 0400
            items: [{key: tls.crt, path: tls.crt}, {key: tls.key, path: tls.key}]
        - {name: records, persistentVolumeClaim: {claimName: scrimmage-records}}
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

`backoffLimit: 0` means the cluster never starts the test a second time: a failed test ends the Job as `Failed`, with one pod.

### Run it

```bash
kubectl apply --server-side -n $NS -f service-accounts.yaml -f records-pvc.yaml \
  -f services.yaml -f network-policies.yaml
kubectl create configmap scrimmage-config -n $NS --from-file=scrimmage.toml
kubectl apply --server-side -n $NS -f agent.yaml
kubectl rollout status deployment/acme-agent -n $NS
kubectl apply --server-side -n $NS -f runner-job.yaml
POD="pod -n $NS -l batch.kubernetes.io/job-name=scrimmage-test"
EXIT='{.items[0].status.containerStatuses[0].state.terminated.exitCode}'
for i in $(seq 360); do   # up to 30 minutes
  kubectl get $POD -o jsonpath="$EXIT" | grep -q . && break; sleep 5
done
kubectl get $POD -o jsonpath="$EXIT"     # 0, 2 or 3, as scrimmage test exits; empty if still running
```

Read the exit code from the pod's status, as above, and the result from the records.
`kubectl logs` mixes any line the runner writes on stderr, such as its waiting message, into its stdout, so it is for a person to read, never for a program.
The loop gives up after 30 minutes, so a pod that never ends, such as one whose image is missing, does not hang it.

### Read the records

The reader pod mounts the records volume read-only and runs nothing else.
It reads the records after the Job and its pod are deleted, too.

```yaml title="reader.yaml"
# Reads the records after the test's Job and its pod are gone. It mounts the volume read-only.
apiVersion: v1
kind: Pod
metadata:
  name: reader
  labels: {app.kubernetes.io/name: scrimmage-reader}
spec:
  serviceAccountName: scrimmage-runner
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 0
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: reader
      image: scrimmage-runner:TAG
      imagePullPolicy: Never
      command: ["sleep", "infinity"]
      resources:
        requests: {cpu: 125m, memory: 128Mi}
        limits: {memory: 256Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: records, mountPath: /records, readOnly: true}
  volumes:
    - name: records
      persistentVolumeClaim: {claimName: scrimmage-records, readOnly: true}
```

```bash
kubectl apply --server-side -n $NS -f reader.yaml
kubectl exec -n $NS reader -- ls /records/scrimmage-runs
kubectl exec -n $NS reader -- scrimmage show /records/scrimmage-runs/<folder>
kubectl exec -n $NS reader -- cat /records/scrimmage-runs/<folder>/result.json
```

Each run's summary names the pod that served it in `claimed_by`, and both digests of its plan, as [a service run's summary line](json-output.md#a-service-runs-summary-line) says.

### When the agent cannot reach the runner

Each case below is tested in the kind cluster with the manifests above and one fault.

| The fault | What the Job's pod ends with |
|---|---|
| The agent's egress policy to the runner allows nothing | exit 3 after 30 s of asking, with the timeout's hint, as in the table above |
| The Service `scrimmage` has a selector that matches no pod | exit 3 after 30 s of asking, with the same timeout's hint |
| The same Service, in a namespace with no NetworkPolicy | exit 3 after 30 s of asking, with the refused hint: `nothing listens there, as when a Kubernetes Service has no ready endpoints; check that the Service's selector matches this runner's pod` |
| No Service named `scrimmage` in the namespace | exit 3 after 30 s of asking, with the name's hint: `the name scrimmage does not resolve where the service runs; check the host in SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace` |

A Service with no endpoints shows as a timeout here, not as a refusal.
With no endpoint, kube-proxy never translates the Service's address to a pod's, so the agent's egress policies, which allow only named pods, drop the packet before kube-proxy can refuse it.
Without an egress policy the same Service refuses the connection, and the hint names its selector.

In each case, the runner prints its waiting line once, sends no setup request and no run, and writes no record and no `result.json` to the volume.
The message is the last line of the pod's log, since there is no result to read.

A policy that cuts the agent off from the runner in the middle of a test does not stop it.
Calico checks a policy against each new connection, and every control request is a new connection, so the cut takes effect at the next request.
The run inside its request at that moment gets a reply from the service, but cannot send its last batch, so it ends `infrastructure error` with `incomplete` true in its summary.
That reply holds no answer: the run's next model call needs a budget reservation from the runner, which cannot be reached, so the call is refused as `control_unreachable`.
The reservation of the call that was in flight stays held, so the runner warns that the run ended still holding part of the budget.
Each later run's claim fails, its gate answers `503 plan-unavailable`, and it ends `infrastructure error` too, after up to 10 s of waiting for the claim.
No run after the cut ends with a result.
The test ends with exit 3, and `result.json` says how many runs were sent and how few were claimed, in `runs_sent` and `claims_received`.

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
