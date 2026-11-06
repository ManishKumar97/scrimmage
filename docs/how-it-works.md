# How it works

Derail has two parts.

- The **runner** is the `derail` command. It reads `derail.toml`, makes a plan for each run, starts your agent, and collects the answer and the record. It never runs inside your agent.
- The **SDK** is Derail's code inside your agent's own Python program. It catches each call and does what the plan says. It is off unless a test switches it on.

## One run, step by step

1. `derail test` starts a small **control server** on your machine (127.0.0.1 only) and makes a random secret for this test.
2. For each run, it stores the run's **plan**: the mocks, the fault, the tools and what is safe to run for real.
3. It starts a new Python program through `derail-run`, in its own process group, with the run's name, the control server's address and the secret in environment variables.
4. `derail-run` puts a folder holding one file, `sitecustomize.py`, first on Python's path. Python runs that file before anything else.
5. The file sees the secret and switches the SDK on. The SDK fetches the plan, checks its signature, and gets ready to swap your tools. It prints one line saying Derail is active.
6. Then it runs any other `sitecustomize.py` your environment has, such as Ubuntu's crash reporter, so your program starts as it always does.
7. Derail's starter calls your function with the question.
8. When your code imports a module named under `tools`, or makes a LangChain tool, the SDK puts a **gate** in front of the function that does the work, a function of the same kind, sync or async. HTTP and MCP libraries get theirs when they are imported.
9. Each tool call meets its gate. The gate decides fault, mock, real or refused, does it, and hands a record line to a background thread that sends lines in small batches.
10. When the program ends, the runner reads the answer, closes the run, and writes the summary line.

Nothing on disk is edited. The swaps exist only in the memory of that one program.

## Model calls

The openai library hands every request to an HTTP library, httpx2, which sends each attempt through one function.
A tool's own requests go through the same function in httpx2 or in httpx.
When your code first imports either one, the SDK swaps that function for a gate.
Derail never imports them itself and does not depend on them, so it uses whatever version your agent has.

The gate sits below the library's retries, so everything above it behaves as it would for real:

1. A request reaches the gate. If its host and path are a model call, the gate labels it. Anything else is a web request, labelled by the host in its own URL, and goes through [the rules for web requests](web-requests.md).
2. The gate reads the tools the request offers, and stops the request or the test if one is not allowed.
3. If a fault fires, the gate builds the reply or error itself and nothing is sent.
4. Otherwise it reserves the call's cost from the budget, which the runner keeps for the whole test, and sends the request.
5. The reply passes through unchanged, while the gate reads its token counts as it goes by. When it ends, the real cost replaces the reservation and the call is recorded.

Every hop of a redirect goes through the same function, so a hop to another model host is decided and budgeted by itself. A redirect to any other host is refused before it is followed, because past that point the gate could not see the call.

Before swapping, the gate checks that the function exists, takes the arguments it expects, and that the library still has the stream types it needs to watch a reply. If a new version of the library changed it, nothing could be gated, so the program stops and the test ends at once with exit code 3, naming the library. If only what faults are built from has changed, the gate keeps refusing and budgeting without faults, and the test still fails. A newer version that passes the check only gets a warning.

## MCP tools

The official MCP client sends every request through one method, `ClientSession.send_request`, on every transport, and FastMCP's client is built on it.
When your code first imports the library, the SDK swaps that method for a gate.

1. A request that is not a tool call goes straight through. From a `tools/list` reply the gate notes each tool's name, whether it declares an output schema, and whether the server marks it read-only.
2. A tool call is labelled with the server's name, which the connection's start gave. The gate decides fault, mock, real or refused.
3. For a mock or a fault, the library's own `send_request` runs with a stand-in for its connection that hands back the reply. Nothing is sent, and the reply passes the same checks a server's reply does. `call_tool` then checks it against the tool's output schema as usual.

Over streamable HTTP each message is also a web POST. The SDK wraps the transport's own methods so that everything they send carries a mark, and the web gate lets those requests through without deciding or counting them. See [MCP tools](mcp.md#over-streamable-http).

Before swapping, the gate checks what it relies on: that `call_tool` goes through `send_request`, and that `send_request` reaches the server through the connection the stand-in replaces. If a new version changed either, a mock could become a real call, so the program stops and the test ends with exit code 3.

## The plan and the secret

The plan is data only: it cannot carry code.
Every request from the SDK and every reply from the control server is signed with the secret (an HMAC), and the secret itself never travels.
So a fake server listening at the same address cannot hand your agent a plan, and a copied request cannot be replayed.

## Failing closed

If the SDK cannot switch on fully, for any reason, the program stops before your agent's code runs.
Your agent never runs in a test without its gates.

The one exception is a child program on a different Python, such as a cloud command-line tool your agent starts.
It inherits the environment but not Derail, so it is left to run as normal.

## Child processes

A child Python program your agent starts on the same Python joins the run: the mocks and the safety rules apply to it, and its calls go into the same record.
Faults fire only in the program the runner started, because each process counts its own calls.
