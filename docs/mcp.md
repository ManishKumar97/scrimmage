# MCP tools

This page is about MCP tools your agent calls.
To drive Scrimmage from a coding agent, see [Driving Scrimmage from a coding agent](mcp-server.md).

MCP, the Model Context Protocol, is a standard way for an agent to call tools that live in a separate program, an MCP server.
You can mock an MCP tool or make it fail, so the agent's own handling of it runs.
It works the same over stdio and over streamable HTTP, with the official `mcp` client and with FastMCP's client.

```python
async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
    await session.initialize()
    await session.list_tools()
    try:
        result = await session.call_tool("search", {"q": question})
    except MCPError:
        return "search is unavailable"
    if result.is_error:
        return "search failed"
```

```toml
[[test]]
name = "docs"
question = "How do refunds work?"
expect = ["refund"]

[[test.mock]]
call = "mcp:docs/search"
returns = {hits = ["Refunds take five days."], total = 1}

[[test.fault]]
call = "mcp:docs/search"
do = "error"
expect = [["unable", "sorry", "unfortunately", "cannot", "can't", "couldn't", "not able", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "i'm currently having trouble", "i'm currently having difficult", "issue retrieving", "issue with retrieving", "issue accessing", "issue with accessing", "timeout", "server error", "server issue", "trying to retrieve", "trying to look up", "trying to access", "trying to check", "i'm experiencing", "i'm still experiencing", "i am experiencing", "i'm currently experiencing", "technical difficult"]]
forbid = ["five days"]

[[test.fault]]
call = "mcp:docs/search"
do = "timeout"
expect = [["unable", "sorry", "unfortunately", "cannot", "can't", "couldn't", "not able", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "i'm currently having trouble", "i'm currently having difficult", "issue retrieving", "issue with retrieving", "issue accessing", "issue with accessing", "timeout", "server error", "server issue", "trying to retrieve", "trying to look up", "trying to access", "trying to check", "i'm experiencing", "i'm still experiencing", "i am experiencing", "i'm currently experiencing", "technical difficult"]]
forbid = ["five days"]
```

The baseline row gets the mocked result.
One fault row gets a failed result and the other an `MCPError`, and the bot's own handling runs in both.
The server never sees any of these calls.

## Naming a tool

| Label | Means |
|---|---|
| `mcp:docs/search` | the tool `search` on the server named `docs` |
| `mcp:search` | the tool `search` on any server |

The server's name is the one it gives itself when the connection starts, such as `MCPServer("docs")`.
It is not the name of the program you start.
A refusal prints the exact label to write, so a test with no mocks is a quick way to find it.

If two servers both have `search`, `mcp:search` could mean either, so the test stops and asks you to name the server.
It stops at the first call after both servers are known.

A tool whose name holds a slash has no short label, because `docs/search` would read as the tool `search` on the server `docs`.
Name it with its server, as `mcp:wiki/docs/search`.
If one label can still be read two ways, such as tool `b/c` on server `a` and tool `c` on server `a/b`, the test stops the same way once both are known.

A server of the 2026 protocol may give no name.
Its tools can only be named `mcp:search`.

## Mocks

`returns` is the value, in TOML.
The result's text is the value, and a table or a list is written as JSON.
If the tool declares an output schema, the value also fills the structured result.

The reply then goes through the library's own checks, so a mock of the wrong shape fails exactly as a real reply of that shape does.
Write the value as the server sends it.
A tool whose declared schema has one `result` field, as FastMCP makes for a tool that returns text, needs `returns = {result = "text"}`.

If your code never listed the tools, Scrimmage lists them first, as the library's check does.
That one `tools/list` request reaches the real server, once for each mocked label in a run.

A mock on a tool the server does not list is a test mistake, and the test ends with exit code 2.
The message names the server's tools, at most 10 of them:

```text
mcp:docs/serch is not a tool server docs lists; its tools are fetch, search. Fix the mock's call, or remove it
```

A listing that has a next page proves nothing, so then the mock answers as usual.
`scrimmage record` applies the same rule: a real call to a tool its server does not list is a test mistake, and its result is not saved.
So a tool a server answers but does not list cannot be mocked or recorded: list it on that server, or run it for real under `real`.

## Faults

| Fault | What the agent sees |
|---|---|
| `timeout` | `MCPError` with code -32001, as if the server never answered in time |
| `error` | a result with `is_error` true, which is how a server reports a tool that failed. FastMCP's `call_tool` turns it into `ToolError` |
| `empty` | a result with no content. On a tool that declares an output schema the library's check fails, as it does for a real empty reply |

`when` can be `always`, `first N` or `call N`, the Nth matching call alone.

## What is refused

Every MCP tool call is refused unless a test mocks it or `real` lists it.
Nothing can tell a read tool from a write tool, so there is no read rule here.
A server can mark a tool as read-only, and the refusal says so, but that mark is only a hint, so it is never trusted.

```toml
[agent]
real = ["mcp:docs/search"]
```

## What passes

Only tool calls are gated.
The connection's own messages pass, and so do requests that are not tool calls: `initialize`, `server/discover`, `ping`, `tools/list`, `prompts/list`, `prompts/get`, `resources/list`, `resources/templates/list`, `resources/read`, `resources/subscribe`, `resources/unsubscribe`, `subscriptions/listen`, `completion/complete` and `logging/setLevel`.
Notifications pass too.
`resources/read`, `prompts/get`, `completion/complete` and `resources/subscribe` also leave a `real` line in the record, labelled such as `mcp:docs/resources/read`, because a server can act on them. See [Safety](safety.md#what-an-mcp-tool-may-do).
Any other request is treated as a tool call, so a request a newer protocol adds is refused until `real` lists it, as `mcp:docs/<method>`.

## Over streamable HTTP

Each MCP message is also a web POST, which the web gate would refuse.
So the transport's own requests are marked, and the web gate lets them through without deciding or counting them.
The GET stream and the DELETE at the end of a session are marked the same way.

The mark is the server's whole endpoint: scheme, host, port and path.
It lasts only while a request is being sent, and ends before the reply is read.
So everything else is still a web request and meets the web gate:

- a POST your own code sends to the same host, on another port or path;
- a request from an event hook on your HTTP client, unless it goes to the endpoint itself while a request is being sent, which passes unseen;
- a token request from an `httpx2.Auth` flow, even on the same host, which is refused unless `real` lists it, such as `"http:docs.acme.internal POST /token"`;
- a request or a model call from a logging, progress or sampling callback, which a model call's budget covers as usual.

A redirect to another path, such as from `/mcp/` to `/mcp`, leaves the endpoint, so the transport's next request is a web write too.
Use the address the server redirects to, or list both `POST` and `DELETE` on that path under `real`.

An `http:` mock or fault on the server's endpoint never fires, because the web gate does not decide those requests.
Use an `mcp:` label instead.

## FastMCP's client for several servers

`Client({"mcpServers": {...}})` with two or more servers starts a router inside your program, named `MCPRouter`, and joins the tool names with the server's, as in `docs_search`.
Your code talks to the router, and the router talks to each server.
So Scrimmage sees two MCP calls for one of yours:

| Call | Label |
|---|---|
| your code to the router | `mcp:MCPRouter/docs_search` |
| the router to the server | `mcp:docs/search` |

The first one is decided first, so a mock on `mcp:docs/search` alone is not enough: the call is refused at `mcp:MCPRouter/docs_search`.
There are two ways to write the test:

- Mock or fail `mcp:MCPRouter/docs_search`. The router never runs, and neither does the server.
- List `mcp:MCPRouter/docs_search` under `real`, and mock, fail or list `mcp:docs/search`. The router really runs, and the second call is decided as usual.

In the second way the router's line says `real`, and also has `forwarded_to`, the label of the call that was really decided.

With one server, FastMCP's client talks to it directly, so there is only the one label.

## Offering tools to a model

A tool in a `tools/list` reply counts as one a gate sees, so you can offer it to the model.
Offer it under the name the server gives it.
A tool the model sees under another name, such as `docs_search`, is reported as a tool no gate sees, and the test stops.

## The record

A call's line has the kind `mcp` and the label as its call.
Its arguments and result go in the `blobs/` folder, with secrets removed.
A real MCP call's line is never dropped, because it could be a write.

## A stdio server and the run

A stdio server you start with `env={**os.environ}` inherits Scrimmage's environment.
On your agent's Python it joins the run like any child process, and prints nothing to the stream that carries the protocol.
On another Python it runs untouched, on any version of `mcp`.
A server started with the library's default environment does not join, and what it does by itself is not gated. Its start is not refused as a blind child, because this gate decides every call to it.
See [An agent run as a script](scripts.md#mcp-servers-over-stdio).
