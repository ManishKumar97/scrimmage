# Limits: tools, MCP and web requests

Limits of the tools Scrimmage gates, of MCP tools, and of web requests and recorded mocks.

## Tools and frameworks

- **Tools, web requests and model calls.** Scrimmage gates the tools you list under `tools`, the LangChain tools it finds by itself, requests sent with httpx or httpx2, and model calls.
  See [Safety](../safety.md).

- **asyncio only for async agents.** An async model call outside asyncio, such as under trio, is refused and ends its run, because Scrimmage cannot budget it.
  Synchronous agents are not affected.

- **One framework.** LangChain tools, which LangGraph runs, have an adapter.
  A tool on another framework is not gated, so offering it to the model stops the test.

- **A tool's function called directly.** `my_tool.func(...)` or `my_tool.coroutine(...)` skips the gate, so nothing refuses or records it.

- **Tool names with no `tools` list.** They are checked after one baseline run, not before it, so a typo costs that one run.
  A tool the agent makes only in some runs may be reported as unknown.

- **An `_arun` that starts `_run` on the default executor itself** counts as two calls and two record lines.
  LangChain's own `_arun` counts as one.

- **A tool changed after the last check.** Classes are checked when they are made, tools when they are made or copied and when they run through LangChain, and all of them before each model request.
  So a class whose `invoke` or `run` is assigned after that, between the model's reply and the call or with no model request at all, runs unchecked.
  A `_run` set on a tool in that gap is still refused.

- **Code written to get around the gate.** None of these is caught: `object.__setattr__` on a copy, a false `__module__` on a class, `BaseTool.invoke = f` patching the library itself, a `__getattribute__` that shows the check a fake `_run`, and a gated tool offered for its schema while a custom node calls a plain function.
  See [Safety](../safety.md#what-the-gates-assume).

- **A tool made with `model_construct`.** Its class is checked, but its name is not known, so offering it to the model stops the test.

- **One unsafe tool blocks its whole name.** A second, gated tool of the same name stops the test too, even if only that one is offered.

- **A plumbing type imported under another name.** If `RunnableConfig as HiddenCfg` is imported only under `TYPE_CHECKING`, a `config: HiddenCfg` argument on a direct `_run` call is recorded, because the written name does not say what it is.

- **A tool with no readable source on Python 3.14.** A function made in the REPL or by `exec`, or kept as a `.pyc` only, has no source to read, so a `config` argument beside an annotation that cannot be resolved is recorded.

- **A wrapper whose `__wrapped__` was set by hand to another function.** On Python 3.14, beside an annotation that cannot be resolved, Scrimmage reads the names of the function `__wrapped__` points to, so a `config` argument can be recorded.

- **LangChain versions.** Tested with langchain-core 1.8.
  A newer one gets a notice; an older one gets none.

- **A library that changes its tool functions** stops the program.
  Faults are not dropped with refusals kept, because the error type is part of what the gate relies on.
  The same goes for `mcp`: a mock must never become a real call.

- **Tools reached by import path.** A tool your code stores in a list or dictionary the moment it is defined keeps the original function, so the gate cannot reach it.
  A marker for those comes later.

- **Methods stored as the class is made.** A class whose metaclass, decorator or class body stores its methods in a table, and calls them from the table, keeps the original functions, so the gate cannot reach them.
  A bound method stored before the module finished loading does the same.
  A method looked up on the instance or the class at the call, such as `getattr(toolkit, name)`, is gated.

- **An alias at the top of a module.** `fetch = get_order` in a module keeps the original function under `fetch`, so a call through `fetch` is not gated or recorded.
  List the alias too, with `{path, name}`.
  An alias in a class body, `fetch = get`, is gated with its method.

- **Two toolkits with the same method names.** Two classes that both have `get_order` need `{path, name}` to tell them apart, and then the label is the name you gave, not the one the model sees, so a model request offering `get_order` is not matched to either.
  Test each toolkit in its own `scrimmage.toml`.

- **Python only.** The SDK must run inside the agent's own Python program.

## MCP tools

- **mcp 2.x only, tested with 2.3.** A program that imports `mcp` 1.x stops with exit code 3, even if it never calls an MCP tool, because 1.x has no place for the gate.
  Install `mcp` 2.x in the test environment.

- **MCP through the official client or a client built on it, such as FastMCP's.** A client of your own that speaks the protocol by itself is not gated at the MCP gate.
  If it sends its own web requests, its POSTs meet the web gate.
  If it is built on `streamable_http_client` and writes its own JSON-RPC messages, they go out through the transport's marked requests, undecided and unrecorded.
  Over stdio nothing sees them.

- **The old SSE transport for MCP.** Its web requests are not marked, so its first POST is refused by the web gate and the run stops.
  Use streamable HTTP or stdio.

- **MCP tools under another name.** A tool the model sees under a name other than the server's, such as `docs_search`, is reported as a tool no gate sees, and the test stops.

- **A hook that posts to the MCP endpoint.** An event hook on the MCP client that sends to the endpoint itself while a request is being sent passes the web gate unseen.

- **A web `GET` or `HEAD` that changes data runs for real.**
  Scrimmage treats those two methods as reads and does not look at what the service does with them.
  Mock the `GET` to keep it from running.
  See [Safety](../safety.md#what-a-web-request-may-do).

- **Only MCP tool calls are gated.** `resources/read`, `prompts/get` and the rest pass, and cannot be mocked or failed.
  A server can act on any of them.
  `resources/read`, `prompts/get`, `completion/complete` and `resources/subscribe` each leave a `real` line.
  See [Safety](../safety.md#what-an-mcp-tool-may-do).

- **Two MCP servers with one name.** Scrimmage cannot tell them apart, and `mcp:docs/search` names both.
  Two servers that give no name are the same case.

- **An MCP label that reads two ways.** Tool `b/c` on server `a` and tool `c` on server `a/b` share the label `mcp:a/b/c`.
  The test stops once both are known, and there is no way to name either one.

- **`mcp:search` before the second server is known.** The ambiguous-label check runs at each call, so a call made before the second server was listed or called goes by that label.

- **A mock on a closed MCP session still answers.** A mock or a fault sends nothing, so it never finds out that the connection has gone.

- **A server that pages its tool list.** Scrimmage reads only the first page.
  When that page has a next page and lacks the mocked tool, the mock answers even if the tool does not exist, so a misspelled label is not caught.
  See [MCP tools](../mcp.md#mocks).

- **A tool its server answers but does not list.** A server that lists tools by permission but takes calls by name can answer a tool it never lists.
  Such a tool cannot be mocked or recorded, and the test stops with exit code 2.
  List the tool on that server, or run it for real under `real`.
  See [MCP tools](../mcp.md#mocks).

## Web requests and recorded mocks

- **POSTs a library sends by itself.** Everything through httpx or httpx2 is gated, so these are refused too until listed: the SDK of another model provider, telemetry exporters, and credential requests such as an OAuth token POST, including one an MCP client sends, even to the MCP server's own host.

- **Two HTTP libraries.** The gate sits in the clients of httpx and httpx2.
  A request sent with requests, urllib.request, http.client, aiohttp or a raw socket, or handed to a transport object by your own code, is not gated, so nothing refuses or mocks it, and a write there is not stopped.
  A request through http.client, requests, urllib3, urllib.request or aiohttp is reported: the report has one warning line per test with the library, the host and the method, `--json` lists it under `notices`, and `scrimmage show` puts it on the run's timeline as `ungated`.
  The report never shows the path, the query, the headers or the body, and it never changes a row's result or the exit code.
  A request is reported when it is sent, even if the connection then fails.
  A request sent when no run is open, or by a thread after its run closed, goes to stderr and to `scrimmage check --service`, not to a test's report.
  A raw socket, pycurl or grpc is not reported.
  Only the `tools` list guards such a write: a listed tool that is neither mocked nor under `real` never runs.
  The web gates stop accidental writes, not code written to get around them.
  See [Safety](../safety.md#what-the-gates-assume).

- **A form the model never sent while recording still misses.** `scrimmage record --runs N` makes a miss rarer, not impossible: at N = 5 a form sent in a quarter of the runs is never seen about a quarter of the time.
  See [Recording more than one run](../recording.md#recording-more-than-one-run).

- **One web mock answers any query, at any N.** Several runs with the same method and path and equal replies give one mock that answers every query and every body.

- **The cross-run web rule leaves out `outside_tool` and `credential` calls.** A request made outside a tool in one run does not block the same request saved inside a tool in another.

- **A reply that changes with time is not written at N above 1.** `--runs 5` can write fewer mocks than `--runs 1` for a tool whose reply holds the time.
  Record it with `--runs 1`, write its mock by hand, or leave it under `real`.

- **Mocks with `args` match arguments exactly.** A model that sends `2.0` for `2`, adds an optional argument, or sends a list in another order misses the mock.
  No looser form exists: no subset, no ignored key, no pattern.
  The miss is reported with the arguments sent and the nearest mock, as a warning when the tool is under `real` by name; otherwise the call is refused, as before.
  The evidence is cleaned by a heuristic, and a secret that fits no pattern under an ordinary key name may print.
  A mock with no `args` answers every call to its tool; see [Mocks that miss](../configuration.md#mocks-that-miss).

- **Request bodies that are not in memory.** A web request sent from a generator or as a multipart upload is decided as usual, but its body is recorded as 0 bytes.

- **A library that loses what mocks are built from.** The test stops with exit code 3 and names the library.
  Until it stops, a mocked read runs for real and a mocked write is refused, which errs on the safe side.

- **`scrimmage record` runs one baseline.** A call a later run would make is not seen; a recorded tool mock answers only the arguments it saw, so a new argument is a miss; see the exact-match entry above.

- **A web mock cannot match a query or a body.** Two GETs that differ only by query, or POSTs with different bodies, get `differs` and are listed by hand when their replies differ.

- **A value that changes between runs is frozen by its mock.** That is what a mock is for.
  A value that changes within one run is listed `nondeterministic`.
