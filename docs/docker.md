# A service in Docker Compose

This page runs an agent's web service in a container, with the runner on the host; how to start the service and its settings are on [An agent inside a web service](services.md).

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
# The acme order bot as a service, with the runner on the host. See the Docker Compose page of the docs.
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
Given `--ip` with no `--name`, DNS names are not limited, so give both, as above.
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

