# How it works

Scrimmage has two parts.

- The **runner** is the `scrimmage` command. It reads `scrimmage.toml`, makes a plan for each run, starts your agent, and collects the answer and the record. It never runs inside your agent.
- The **SDK** is Scrimmage's code inside your agent's own Python program. It catches each call and does what the plan says. It is off unless a test switches it on.

## One run, step by step

1. `scrimmage test` starts a small **control server** on your machine (127.0.0.1 only) and makes a random secret for this test, the run token.
2. For each run, it stores the run's **plan**: the mocks, the fault, the tools and what is safe to run for real.
3. It starts a new program through `scrimmage-run`, in its own process group, with the run's name, the control server's address and the secret in environment variables. For a function that program is Scrimmage's starter. For a script it is your command, with the question as one argument and no shell.
4. `scrimmage-run` puts a folder holding one file, `sitecustomize.py`, first on Python's path. Python runs that file before anything else.
5. The file sees the secret and switches the SDK on. The SDK fetches the plan, checks its signature, and gets ready to swap your tools. It prints one line saying Scrimmage is active.
6. Then it runs any other `sitecustomize.py` your environment has, such as Ubuntu's crash reporter, so your program starts as it always does.
7. Your script starts, or Scrimmage's starter calls your function with the question.
8. When your code imports a module named under `tools`, or makes a LangChain tool, the SDK puts a **gate** in front of the function that does the work, a function of the same kind, sync or async. A method is gated on its class, so every instance goes through the gate. HTTP and MCP libraries get theirs when they are imported.
9. Each tool call meets its gate. The gate decides fault, mock, real or refused, does it, and hands a record line to a background thread that sends lines in small batches.
10. When the program ends, the runner stops anything left in its process group, reads the answer, closes the run, and writes the summary line.

Nothing on disk is edited. The swaps exist only in the memory of that one program.

## Model calls

The openai library hands every request to an HTTP library, httpx2, which sends each attempt through one function.
A tool's own requests go through the same function in httpx2 or in httpx.
When your code first imports either one, the SDK swaps that function for a gate.
Scrimmage never imports them itself and does not depend on them, so it uses whatever version your agent has.

The gate sits below the library's retries, so everything above it behaves as it would for real:

1. A request reaches the gate. If its host and path are a model call, the gate labels it. Anything else is a web request, labelled by the host in its own URL, and goes through [the rules for web requests](web-requests.md).
2. The gate reads the tools the request offers, and stops the request or the test if one is not allowed.
3. If a fault fires, the gate builds the reply or error itself and nothing is sent.
4. Otherwise it reserves the call's cost from the budget, which the runner keeps for the whole test, and sends the request.
5. The reply passes through unchanged, while the gate reads its token counts as it goes by. When it ends, the real cost replaces the reservation and the call is recorded.

Every hop of a redirect goes through the same function, so a hop to another model host is decided and budgeted by itself. A redirect to any other host is refused before it is followed, because past that point the gate could not see the call.

Before swapping, the gate checks that the function exists, takes the arguments it expects, and that the library still has the stream types it needs to watch a reply. If a new version of the library changed it, nothing could be gated, so the program stops and the test ends at once with exit code 3, naming the library. If only what faults are built from has changed, the gate keeps refusing and budgeting without faults, and the test still fails. A newer version that passes the check only gets a warning.

## MCP tools

The official MCP client sends every tool call through one method, `ClientSession.send_request`, on every transport, and FastMCP's client is built on it.
Almost every other request goes through it too; the connection's own messages and notifications do not.
When your code first imports the library, the SDK swaps that method for a gate.

1. A request that is not a tool call goes straight through. From a `tools/list` reply the gate notes each tool's name, whether it declares an output schema, and whether the server marks it read-only.
2. A tool call is labelled with the server's name, which the connection's start gave. The gate decides fault, mock, real or refused.
3. For a mock or a fault, the library's own `send_request` runs with a stand-in for its connection that hands back the reply. Nothing is sent, and the reply passes the same checks a server's reply does. `call_tool` then checks it against the tool's output schema as usual.

Over streamable HTTP each message is also a web POST. The SDK wraps the functions the transport sends with, so each request carries a mark while it is being sent, and the web gate lets a marked request to that endpoint through without deciding or counting it. See [MCP tools](mcp.md#over-streamable-http).

Before swapping, the gate checks what it relies on: that `call_tool` goes through `send_request`, that `send_request` reaches the server through the connection the stand-in replaces, and the names it reads. If a new version changed one of those, a mock could become a real call, so the program stops and the test ends with exit code 3. `mcp` 1.x stops the same way.

## The plan and the secret

The plan is data only: it cannot carry code.
Every request from the SDK and every reply from the control server is signed with the secret (an HMAC), and the secret itself never travels.
So a fake server listening at the same address cannot hand your agent a plan, and a copied request cannot be replayed.

## Failing closed

If the SDK cannot switch on fully, for any reason, the program stops before your agent's code runs.
A program that never runs the startup file cannot stop itself, so `command` is checked when the file is read: `python -E`, `-I` or `-S` there is a mistake in the test, exit 2, before any run.
A program that reaches no gate some other way, such as `env PYTHONPATH=. python bot.py`, runs for real, and the run reads infrastructure error. See [Limits](limits.md).

The one exception is a child program on a different Python, such as a cloud command-line tool your agent starts.
It inherits the environment, but it is not the run's Python, so it is left to run as normal.
Only a listed tool is kept from it: importing that tool's module fails, and the run says so.

## Child processes

Every Python program your agent starts inherits `PYTHONPATH`, so it runs Scrimmage's startup file too.

The first Python in a run that has the SDK claims the run. It creates a file beside the answer file with `O_CREAT | O_EXCL`, so of any number of Pythons one wins, even siblings under a shell that share no environment.
It is not the process with a given id, because a wrapper such as `uv run` starts Python as its own child.
The file and `SCRIMMAGE_PYTHON` hold what that Python is: its prefix and its version.
A later Python compares itself with that value. `python` and `python3` in one venv are the same Python, even if `venv --copies` made them two files. Two venvs on one binary are not, because each has its own packages.
A Python with no SDK cannot claim. It runs as usual, unless `scrimmage-run` became it directly: then it is the program under test for certain, and it stops.

A child on the same Python joins the run, in three steps:

1. At startup it swaps the tools named in `SCRIMMAGE_TOOLS`, because your code imports them before any plan could arrive. It fetches nothing and prints nothing.
2. At its first gated call it fetches the plan. If it cannot, every call is refused. Unless `children` is set, it drops the plan's faults, because each process counts its own calls.
3. It has no background thread. It sends each record line as it makes it, with the number of lines so far, so the record has no gap however the process ends.

A child's lines are sent with no lock over the network, because each send has its own connection and the store writes a line once.

A forked child starts at step 2 or 3, depending on whether its parent had the plan, with new locks and an empty queue, because the sender thread does not survive a fork. See [An agent run as a script](scripts.md#child-processes).

## Child processes Scrimmage cannot reach

Each process that joined adds an audit hook, which Python calls for every event, and which ignores all but three: `subprocess.Popen`, `os.exec` and `os.posix_spawn`. `os.spawnv` is an exec in a forked child here.
For those it asks whether the target is this run's Python, by its name or its shebang, and whether it would run blind: no Scrimmage variables in its environment, or a flag that skips the startup file.
If so it decides a call labelled `process:python`, which is refused unless `real` lists it.
An exception from an audit hook stops the operation, so the child never starts. The MCP gate marks its own start of a stdio server, so that one passes.

`os.exec` ends the program with no exit steps, so the same hook sends the record lines first.

## Stopping a run

The runner stops a whole process group, and waits on the group, not on your program: a child can outlive your program, and can ignore SIGTERM after it has gone.

SIGTERM reaches every process in the group. Each one that joined the run gives any call still going on a line, sends its lines, and then ends by the same signal, so the exit status is what it would have been without Scrimmage.
The handler runs on your program's main thread, so a thread does the sending, and the wait for it is limited to four seconds, under the five the runner gives before SIGKILL.

Then the runner sweeps `/proc` for any process that left the group but still holds the exact `SCRIMMAGE_RUN` of this run in its own environment. It kills those and records each one. A process it misses gets a 409 on its next record line, which refuses all its later calls.
