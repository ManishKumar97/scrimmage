# Web requests inside a tool

A tool usually does its work through a web request.
You can mock or fail that request instead of the whole tool, so the tool's own error handling runs.

```python
def check_inventory(sku: str) -> str:
    try:
        reply = httpx.get(f"https://inventory.acme.internal/stock/{sku}", timeout=2)
        reply.raise_for_status()
    except httpx.HTTPError:
        return "inventory unavailable"
    return f"{reply.json()['count']} in stock"
```

```toml
[[test]]
name = "stock"
question = "How many SKU-42 are left?"
expect = ["12"]

[[test.mock]]
call = "http:inventory.acme.internal"
method = "GET"
path = "/stock/SKU-42"
json = {count = 12}

[[test.fault]]
call = "http:inventory.acme.internal"
do = "status 503"
expect = [["sorry", "unable"]]
forbid = ["12"]

[[test.fault]]
call = "http:inventory.acme.internal"
do = "timeout"
expect = [["sorry", "unable"]]
forbid = ["12"]
```

The baseline row gets the mocked reply.
One fault row gets a 503 and the other a `ReadTimeout`, and in both the code in the `except` runs.

## What is covered

Scrimmage gates requests sent with httpx and httpx2, sync and async, from anywhere in the program.
The gate sits at the client's single attempt.
Retries, redirects, cookies and response hooks all sit above it, so they treat an injected reply as a real one.
`raise_for_status` in a response hook raises the same error on an injected 503 as on a real one.

Other HTTP libraries, such as requests, urllib and aiohttp, are not gated. See [Limits](limits.md).
A write through one of them reaches the real system, and a test does not record or refuse it.
To make the call safe, move it to httpx.
Or list the tool that makes it under `tools`: a listed tool that is neither mocked nor under `real` never runs, so the write inside it never happens.

A POST you did not write yourself goes through the same rule.
An OAuth token request, a telemetry exporter, an MCP server over HTTP and another provider's SDK are all refused until mocked or listed, and a listed provider call is not budgeted. See [Limits](limits.md).

## The label

A request's label is `http:` and the host in the request's own URL, in lower case, such as `http:inventory.acme.internal`.
The port, any password in the URL and any proxy are not part of it, and a label with a port is rejected.
An IPv6 address is written `http:[::1]`.
A request sent through a proxy is labelled by the host it asks for, not by the proxy.

A mock or a fault can be narrowed with `method` and `path`.
A `path` is matched exactly as the URL writes it, percent-encoded, and never includes a query string.
So `/a%20b` matches a request for `/a b`, and `/a/b` does not match `/a%2Fb`.
An international host is named by its `xn--` form.

## What a request gets

For every request, in this order:

1. **Fault**: a fault that names it fires.
2. **Mock**: a mock that names it answers. The first match wins. Nothing is sent.
3. **Real**: it runs for real if `real` lists it.
4. **Reads**: `GET` and `HEAD` run for real.
5. **Refused**: anything else is refused. See [Safety](safety.md).

Under `real` you choose how much to allow:

| Entry | Allows |
|---|---|
| `http:orders.acme.internal` | every method to that host |
| `http:orders.acme.internal POST` | that method to any path |
| `http:orders.acme.internal POST /refunds` | that method to that one path |

`http:*` is not allowed. Name each host.

Each hop of a redirect is decided on its own, so a redirect cannot carry a write past a rule.

## Mocks

| Setting | What it does |
|---|---|
| `status` | The status of the reply. 200 unless you set it. |
| `json` | The body, as JSON. Sets `Content-Type: application/json`. |
| `text` | The body, as text. Sets `Content-Type: text/plain`. |
| `headers` | Extra reply headers. Yours win over the `Content-Type` above. `Content-Length` is written from the body, and `Content-Encoding` and `Transfer-Encoding` would not match it, so none of the three can be set here. |

With neither `json` nor `text` the reply has an empty body.
A `Set-Cookie` header reaches the client's cookie jar, as it would from a real reply.

## Faults

| Fault | What your code sees |
|---|---|
| `status N` | a real reply with that status, from 400 to 599, and a short plain-text body |
| `timeout` | the library's own `ReadTimeout` |
| `refused` | the library's own `ConnectError`, caused by a `ConnectionRefusedError`, as when nothing listens on the port |
| `reset` | the library's own `ReadError`, caused by a `ConnectionResetError`, as when the server drops the connection |
| `dns` | the library's own `ConnectError`, caused by a `socket.gaierror`, as when the name does not resolve |
| `empty` | a 200 with no body and no `Content-Type`, as a broken backend or proxy sends |
| `bad body` | a 200 with `Content-Type: application/json; charset=utf-8` and the body `{"data": [{"id": 1, "name": "`, so reading it as JSON raises `json.JSONDecodeError` |

The message is the one a real failure has on that path.
The `dns` message is Linux's, from glibc. On macOS a real one reads differently, while the class and the errno constant, `socket.EAI_NONAME`, hold.
On the async path a refused connection reads `All connection attempts failed`, and a reset has no message, as a real one does there.
The request is never sent, so a `reset` never reaches the server.
`empty` and `bad body` are not sent either, so a write under them never runs.
A `HEAD` request gets the status and the headers, with no body.

`when` can be `always`, `first N` or `call N`, the Nth matching request alone.
`requests N` is for model calls only.

## Mistakes it catches

Mocking `tool:check_inventory` and failing `http:inventory.acme.internal` in the same test is a test mistake.
The mock replaces the whole tool, so the request never happens and the fault never fires.
Mock or fail one level, not both.

A real request to a host the test has mocks for, when none of them matched its method and path, gets a warning in the report.
It usually means a path in the test does not match the one the code asks for.

A web mock that no row used is a test mistake, exit code 2.
A web label has nothing to be checked against, so this is what catches a misspelled host.

## What is recorded

The record holds the label, the method, the path, the status, and the bodies with known secrets removed.
Headers, cookies, query strings and a password in the URL are never recorded.
A URL quoted in any recorded text has its query string, fragment and password replaced by `[removed]`.
Text joined to such a URL with no space, such as a list of URLs split by commas, can go with it.
A URL whose slashes are escaped, as in `https:\/\/`, is not seen as a URL.
A failed web request records only the error's type, since a client's message can quote a header.
A path is recorded as it was sent, with known secrets removed, so a key of an unknown shape in a path stays in the record.
A body sent from a generator or as a multipart upload is recorded as 0 bytes, because reading it would use it up.
The `.log` beside each run holds what your program printed, unchanged.
