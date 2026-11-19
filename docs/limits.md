# Limits of this version

## Will Scrimmage work for your agent?

Six limits decide this.
Check them first.
Every other limit is listed below, grouped by how your agent runs.

- **OpenAI only.** Model calls are understood for OpenAI's API and for gateways that speak its format; a call to another provider stops the test until you list it.
  Either name a gateway that speaks OpenAI's format under `model_hosts`, or list the provider's host under `real`, where its calls run with no budget and no faults; see [Use a gateway](model-faults.md#use-a-gateway).
- **LangChain only.** LangChain tools, which LangGraph runs, are found by themselves; a tool on another framework is not gated.
  List each tool's function under `tools`; see [the `tools` setting](configuration.md#agent).
- **httpx only.** Web requests through httpx and httpx2 are gated; `requests`, `aiohttp`, `urllib` and raw sockets run unrecorded and ungated.
  A write through `requests` or `aiohttp` reaches the real system.
  The openai SDK uses httpx2, so model calls are covered.
  Move the call to httpx or httpx2, or list the tool that makes it under `tools`; see [What is covered](web-requests.md#what-is-covered).
- **No streaming replies.** A service that streams its answer stops the test with exit 2, and a streamed model or web reply cannot take `replace` or `append`.
  Test a route that returns one reply; see [Each run](services.md#each-run).
- **Rules match text, not meaning.** `expect` finds text, so "shipped" passes inside "has not shipped".
  Write a `forbid` for the wrong answer too; see [How text is compared](judging.md#how-text-is-compared).
- **Install from git.** Scrimmage is not on PyPI yet; the name there holds an empty placeholder.
  Install it from a clone; see [From a clone](install.md#from-a-clone).

If none of these applies, the rest of this page is detail for your use case.

## Writing and checking a test

- **`scrimmage check` does not import your code.** It finds a tool name that is not in `tools`, but not a misspelled import path such as `acme.tools:get_ordr`; the first run of `scrimmage test` finds that. A static lookup would call a tool made by assignment or a star import a mistake, and then `check` would fail a file `test` runs.

- **`scrimmage init --probe` can run calls a test baseline would refuse.** It runs the function you name up to its first model request with no tools list, so a plain tool function called before then runs for real, and so does a write through a library Scrimmage does not gate, such as `requests`. Web writes through httpx, LangChain tools and MCP tools are still refused. A script is never probed. See [Drafting a test file](init.md#what-the-probe-can-do).

- **`scrimmage init` reads the code, so it only guesses.** A tool made at run time, in a dict comprehension or by a framework has no mark the read can see, and the entry point is chosen by a heuristic. `--probe` finds tools by what the model is offered, at the first model request only: a tool offered later is not seen.

- **The editor schema checks shape only.** `scrimmage schema` cannot see a tool name against `tools`, a type outside your packages or a program on your `PATH`. `scrimmage check` decides.

- **The mock type check reads the outer type and the first item.** A mock's value is checked against its tool's return annotation by its outer type, such as `list` or `dict`, and, when the items are a class, by its first item only. So `-> list[str]` with a number in the list gets no warning. Items are read only for `list[X]`, `tuple[X, ...]` and `dict[str, X]` themselves: the table items of `-> list[Order] | None` or `-> Sequence[Order]` get no warning. A wrong value is a warning, never a mistake, because the annotation can be the wrong one.

- **`--json` prints at the end.** One document, when the command ends; nothing is printed while a test runs.

## A function or a script agent

- **`process:sh` lets any shell command through.** A start is decided by the program's name alone, so listing `process:sh` makes every `shell=True` call and every `os.system` real, whatever the command.

- **A command that clears Scrimmage's path.** `env PYTHONPATH=. python bot.py` replaces the startup folder, so the program runs with no gates, its writes are real, and only then does the run read infrastructure error. Add to `PYTHONPATH` in the environment of `scrimmage test` instead. A flag that does the same, `python -E`, is refused when the file is read.

- **A blind child not started from Python.** A Python child with its own `env`, or with `-I`, `-E` or `-S`, is refused when your Python code starts it. Only those starts are seen, so `sh -c 'python x'`, `env -i python x`, `os.system`, and any exec from a program that is not Python, such as a shell script or a wrapper, are missed: the child runs with no gate and no line. A shell script that runs Python has the same gap if it clears the environment.

- **A blind child on another Python.** A Python that is not the run's, with its own `env`, passes the check and is not kept from a listed tool. That includes a venv elsewhere made from the same binary. The base binary of your venv counts as this Python, and is refused.

- **multiprocessing after an edit of the environment.** `spawn` and `forkserver` start their workers through a call no audit event reports. A worker inherits `os.environ`, so it joins, unless your code removed `PYTHONPATH` or `SCRIMMAGE_TOKEN` from it first. Then it runs blind, with no refusal.

- **Refusals that can surprise you.** Each is a `process:python` refusal, fixed by the message:
  `pip install` of a source package on the run's Python, as pip builds it under a `PYTHONPATH` of its own;
  `env={**os.environ, "PYTHONPATH": "..."}`, which replaces Scrimmage's folder, so add to the value instead;
  a tool on the run's Python with a shebang such as `#!/usr/bin/python3 -Es`, as on a system venv.

- **A tool in a file another program runs.** `python -m cProfile bot.py` and `runpy.run_path("bot.py")` run `bot.py` as `__main__` with a program Scrimmage does not read as `bot.py`, so a listed tool defined in it is not stopped and writes for real, exit 0. Keep tools in a module.

- **A wrapper that is a Python program with no Scrimmage.** `poetry run python bot.py` stops with exit code 3, as poetry is the program under test. Use a shell script: see [An agent run as a script](scripts.md#the-command).

- **What a stdio server does by itself.** A server on the library's default environment does not join. The MCP gate decides every call to it, but a write the server makes is not gated.

- **Python is told by name or shebang.** A program that embeds Python under another name is not seen as Python.

- **The first Python with the SDK owns the run.** In a wrapper, another venv with Scrimmage installed that runs first takes the run, and your bot, on a different Python, is then a program on another Python: only listed tools are kept from it. A step on the same Python before the bot makes the bot a child, so no fault fires in it. With a wrapper and no SDK, a Python that is really your agent runs ungated until the run reads infrastructure error, with listed tools kept out.

- **A root under `uv run` that execs itself.** It comes back as a child, so it fires no faults. A root that scrimmage-run became directly stays the root.

- **Programs on another Python.** They are not gated, so a web write or an MCP call from one is not stopped or recorded. Only a tool under `tools` is kept from them, by module name and file ending. A copy of its code is not. With `children` set, one that has Scrimmage installed joins, and a stdio server on `mcp` 1.x then stops with exit code 3.

- **A daemon that starts its own session.** A child that calls `os.setsid`, or is started with `start_new_session=True`, leaves the run's process group, so the group stop does not reach it. A sweep of `/proc` after the run finds it by the exact `SCRIMMAGE_RUN` in its environment, kills it and records it. Until then it can run. A process that starts with a clean environment holds no such variable and is not found. A system with no `/proc`, such as macOS, has no sweep: the daemon keeps running, a 409 on its next line refuses its later calls while the test goes on, and a `real` call after `scrimmage test` has ended runs for real. A double fork with no new session stays in the group and is stopped.

- **A last line of output shared with a child.** The last printed line is read from one file, so a child that prints after your program comes last, and nothing says so. Print the answer last or use the answer file. See [An agent run as a script](scripts.md#the-answer).

- **`first N` in children.** With `children` set each process counts its own calls, so `first 1` fires once per process, and `call 2` on the second call of each.

- **A child's gates wait for the control server.** A child has no sender thread, so each of its gated calls waits for its record line to be sent, and an async gate in a child holds up the event loop for that long.

- **`os.fork` warns on Python 3.12 and later.** The SDK's sender thread makes your program multi-threaded, so Python prints a `DeprecationWarning` at `os.fork` in your own code, and raises it under `-W error`.

- **Stopping needs Python to run.** A program stuck in native code that holds Python's lock cannot run the SIGTERM handler, so SIGKILL ends it at five seconds, and the lines of its last 100 ms are lost. The same goes for a program whose own handler does not exit.

- **SIGTERM taken by a thread that is not Scrimmage's.** Linux gives a signal sent to a process to any thread that does not block it, and can pass over a main thread that is waiting. Scrimmage's own threads block SIGTERM and SIGINT, so it never goes to one of them. A thread your code or a library started, such as asyncio's worker threads, can still take it. If your main thread is then waiting on a call that never ends, the handler does not run until that call returns, so SIGKILL ends the program at five seconds and the call has no line. Block the two signals in your own threads with `signal.pthread_sigmask` to avoid it, but not in a thread that starts a program, which would start with them blocked too.

- **A hung call under your own SIGTERM handler.** Your handler replaces the SDK's, so a call going on when it runs gets a line only if the handler exits normally through Python's exit steps. If it ends with `os._exit` or the signal, or never returns, the call has no line.

- **A root that execs another program.** Its lines are sent first, and the program's last printed line can be the answer. If that program is not Python, nothing after the exec is gated or recorded. If it never prints, the run reads as crashed.

- **The startup file needs Python 3.8 or later.** On an older Python 3, a child's startup prints an error. Only 3.11 to 3.14 are run in tests; 3.8 syntax is checked by parsing.

- **A call with no plan on no server.** If the control server cannot be reached, a child's refusal has nowhere to go: the call is still refused, and the run ends as usual, with no line.

## An agent inside a web service

- **A service run is stricter than a script about processes.** A script's child joins its run. In a service, a run is one request in a process that serves others, so every process start under a run is refused unless `real` lists its program as `process:<program>`, and so are the forks, `multiprocessing` starts and sends, process pools and subinterpreters that [Safety](safety.md#an-agent-inside-a-web-service) lists by name. A Python child never joins a service run.

- **Signals are not gated.** `os.kill`, `os.killpg` and `signal.pthread_kill` reach another process with no decision: a run can stop a process, or make one start again. A relaunch of Python's resource tracker after a run killed it is refused like any other start; see [Safety](safety.md#an-agent-inside-a-web-service).

- **A run's changes to its process carry over.** The working folder, `os.environ`, signal handlers and every other object the service process shares belong to the whole process, so a run that changes them changes them for later plain requests, for work outside any request and for any process started after it, a relaunch of Python's resource tracker included.
  That covers an item waiting in a queue's buffer, which a run can change before the feeder pickles it, and pickler reducers a run registers with `ForkingPickler.register` or `copyreg`, which then run for every later pickling.

- **Other ways to another process are not gated.** Shared memory (`multiprocessing.Value`, `Array`, `shared_memory`), a lock or a semaphore shared with another process, a socket or a file another process reads, and any send point not on that list reach another process with no decision and no line.

- **A queue's private parts are not gated.** Code that takes a `multiprocessing` queue's lock or semaphore, or empties its buffer, can stall the queue for every request, and for the processes that share it, but sends them nothing. Code that puts back the pickler a queue's feeder uses removes the feeder's check: its sends are then decided as any other send, so one made while no run is open passes.

- **A process pool's `close()`, `terminate()` or `shutdown()` inside a run is refused.** Each sends to the pool's processes. The refusal comes before the stop changes anything, so the pool still works. Stop pools at the service's shutdown, outside any request.

- **`--loop asyncio` on Python 3.11.** On 3.11 Scrimmage watches each task a run makes through the loop's `create_task`, which uvloop's loop does not let it do. A service on uvloop stops at its start with a message naming `--loop asyncio`. From 3.12, uvloop works.

- **On Python 3.11, a task built outside `create_task` is not seen at a run's close.** A run whose task outlives it is an infrastructure error. On 3.11 that check sees tasks made with `asyncio.create_task` or `loop.create_task` only; from 3.12 it reads every task on every loop.

- **Each control request is a new connection.** The service opens a TCP connection and, over TLS, a TLS handshake for every claim, record batch and budget ask, and a closed connection sits in the host's connection-tracking table for 120 seconds. With the usual table of 262,144 entries it fills at about 2,200 new connections a second; the kernel then drops new connections without a reply, and runs end as `plan-unavailable` infrastructure errors. Watch `control_p99_ms` and the report's `control server:` lines. Keeping connections alive is a later step.

- **A control path cut mid-test does not stop the test.** When a NetworkPolicy or a firewall drops the agent's traffic to the runner after the probe, every later run is still sent, and each waits up to 10 seconds for its claim before its gate answers `503 plan-unavailable`. Every run after the cut is an infrastructure error and the test ends with exit 3, but a long test takes about 10 seconds a run to get there.

- **A clock jump during a test can show as a lost connection.** A service whose clock moves more than 60 seconds from the runner's after the probe has every signed request refused. A claim says so, and its run names the clock, but a record batch over 4 MB is refused before its body is read, so the service sees a closed connection and the run's record is incomplete.

- **One runner per service at a time.** Two tests against one service, each with its own control server, are not detected. Start one test at a time.

- **Work outside any request has a cost while a test runs.** While a run is open, a background job, a lazy worker or a thread a plain request left running cannot be told from a run's own work. So its writes are refused unless `background_real` lists them, and each such call, a read included, ends every run open at that moment as an infrastructure error. A team whose service does background work while tests run lists that work under `background_real`.

- **A long plain request keeps the threads it started plain until it ends.** A thread a request without a run starts, such as a worker a streaming reply or a WebSocket starts and feeds, runs as that request while it is open, so work a run hands to that thread runs for real. Once the request ends, the same work meets the outside rules.

- **gunicorn is not covered, with `UvicornWorker` either.** Its workers are forks of one process, and a fork has none of Scrimmage's threads, so each such worker stops at its first request and says so. Run the app with `uvicorn --workers N`.

- **A service row's runs go one after another.** Each run of a row is sent once the row's run before it is settled, after its grace and wait, so a refusal stops its row before the next run. Other rows run beside it, so `parallel` is spent across rows, not within one.

- **A call `background_real` lets through is decided once, with what it runs inside.** Outside any request, a `multiprocessing` send, pool task or subinterpreter call listed as `process:multiprocessing` is decided when it is made. What runs inside it is not decided again: the pickling of its arguments (an object's own `__reduce__`), the unpickling of a pool's results and of a subinterpreter's error, a `context=` or `Process` factory it is given, and a pool's `initializer`, run again when a worker is replaced, as `maxtasksperchild` does. A program started or a send made there has no line. Under a run each such call is refused before any of it runs.

- **A start made below multiprocessing's own calls is not seen before Python 3.14.** `_posixsubprocess.fork_exec` called directly, or a forkserver's `Process._Popen` called by hand, raises no audit event and passes no gate before 3.14. `Process.start`, `subprocess`, `os` and `multiprocessing.util.spawnv_passfds` are all decided. Code written to get around the gate is not caught; see [Safety](safety.md#what-the-gates-assume).

- **The setup request lists only what it saw.** Outside work that did not happen while the setup request was open is not in its list, and surfaces later as an infrastructure error naming the entry to add.

- **A lazy worker started inside a run.** A thread a run starts and keeps for later requests, such as a batcher, holds that run after it ends: its calls are refused, and the next run claimed in that process is an infrastructure error that names the thread. Start such workers at startup, outside any request.

- **The Starlette gate alone lets an outer middleware see the run header.** Under uvicorn the gate is outermost. Without uvicorn's gate, an ASGI middleware wrapped around the Starlette app, outside it, sees `x-scrimmage-run` before the gate removes it.

- **hypercorn, `--http zttp` and `--http2` are not covered.** zttp and HTTP/2 stop the service at its start. hypercorn is not tested.

- **Stopping the service mid-run.** uvicorn installs its own SIGTERM handler, so runs open when it stops lose their last lines, and read as infrastructure errors.

- **A killed runner leaves the copy's workers on macOS.** On Linux, the program `start` names and uvicorn's workers are killed when the process that started them dies, so a runner killed with SIGKILL takes a uvicorn copy with it. macOS has no such call: there the workers keep serving the port until you stop them.

- **A killed runner leaves the copy's other programs.** On Linux, a runner killed with SIGKILL takes only the program `start` names and the workers of a uvicorn started directly by it. A program that is not Python, such as `sleep 600 &` in a shell wrapper, a uvicorn under such a wrapper, and a helper your app starts, keep running until you stop them. Scrimmage does not ask them to die with the copy: Linux would kill such a program when the thread that started it ends, and a helper started from a short-lived thread must keep running. The end of a test, Ctrl-C and SIGTERM stop every program in the copy's process group.

- **A killed runner leaves its token folder.** A runner killed with SIGKILL cannot delete the `scrimmage-token-` folder it made in the temporary folder. The token in it opens nothing once that runner is gone, and the system clears the folder with the rest of its temporary files.

- **Another process can take the port before the copy binds it.** Scrimmage picks a free port, and the copy binds it a moment later. If another process takes it in between, the copy exits, the test stops with exit 3 and the copy's last lines, which say the address is already in use, and a second run picks another port. There is no retry.

- **A process that leaves the copy's group with `setsid` is not stopped.** With `start`, Scrimmage stops the copy of the service by its process group, so a process the service starts in a session of its own keeps running after the test.

- **A service run refuses every process start, a library's own too.** The openai library runs `uname -p` through `platform` to name its machine, so a service whose agent calls OpenAI needs `"process:uname"` under `real`. The setup request finds it before any run and names the entry to add.

- **A service run that queues 10,000 record lines it cannot send waits.** Each run keeps up to 10,000 lines while the runner is away. Once full, a refusal or a model call waits for room in the request that made it, so that request slows until the runner answers again; other lines are dropped and counted.

## In Docker Compose

Also read the control-path limits under An agent inside a web service.

- **A client without the token can hold the control server's connections.** When `control` is not loopback, any container or pod on the host can open the runner's 64 connections and send nothing. Each is cut after 15 seconds, and while all are held the runs end as `plan-unavailable` infrastructure errors, never as results. A network policy that limits who may connect is the fix.

## In a Kubernetes cluster

Also read the control-path limits under An agent inside a web service.

- **One node.** The tests run one kind node.
  A node failure, several nodes and the runner's pod moving between nodes are not tested.

- **No encryption between nodes.** The runner's requests to the agent are `http://`, and Calico carries pod traffic between nodes unencrypted unless its WireGuard encryption is on.
  The tests do not turn it on.

- **The runner runs inside the cluster.** A runner outside it cannot serve the agent's pods: `kubectl port-forward` carries traffic one way only.

- **Nothing between the runner and the agent.** A service mesh or an ingress in front of the agent is not tested; the runner calls the agent's Service directly.

- **One Job at a time against one `scrimmage` Service.** A second Job behind the same Service fails the ping's runner id check.

- **The probe reaches one replica.** A second replica whose control channel is broken is not named before the test; its runs end as infrastructure errors.

- **The default certificate lasts one day.** A runner that starts after it expires exits 2, and a test that outlives it has new handshakes refused; make a new one as [When the certificate expires](kubernetes.md#when-the-certificate-expires) shows.

- **A new certificate needs the agent's pods restarted.** The SDK reads `SCRIMMAGE_CONTROL_CA` once, at its start, and each `scrimmage cert` makes a new authority.
  With cert-manager, a renewed server certificate keeps the same authority, so it needs nothing.

- **With cert-manager, a certificate renewed during a test is seen at the next test.** The runner reads its certificate and key at its start.
  A runner that starts while the kubelet swaps the Secret's files for a renewal can read the new certificate with the old key; it then stops with exit 2, saying the two cannot be used together, and a restart fixes it.

- **With cert-manager, the control authority's name constraint covers every namespace.** The default authority from `scrimmage cert` names one namespace's three names; cert-manager's permits `scrimmage`, `svc` and `cluster.local`, and every name that ends with one of them.
  A DNS name constraint is a suffix rule, so it cannot narrow the authority to one namespace: every `scrimmage.<namespace>.svc` is inside it.
  For that reason the control certificate names `scrimmage`, `scrimmage.<namespace>.svc` and `scrimmage.<namespace>.svc.cluster.local`, and not `scrimmage.<namespace>`.
  It excludes every IP address, so a certificate from it that names an address is refused; give `SCRIMMAGE_CONTROL_URL` a host name, as the shipped `https://scrimmage:7700` does.
  The authority is trusted only through `SCRIMMAGE_CONTROL_CA`, and its key lives only in `scrimmage-pki`.

- **A name lookup can carry data out.** The `dns` policy lets every pod ask cluster DNS, and CoreDNS forwards a name it does not serve to the resolvers upstream.
  So a process in any pod can put data in a name it looks up, and no NetworkPolicy stops it.
  A CoreDNS that answers only cluster names, or a DNS firewall, closes that path; the tests do neither.

- **Not part of this version.** trust-manager, a Helm chart for the runner, a published and signed runner image, and a production storage class for the records.

## General

### Programs and processes

- **Three agent shapes.** The agent is a Python function, a Python script, or an ASGI web service under uvicorn, such as a FastAPI app. See [An agent inside a web service](services.md).

- **Code that rewrites private internals is out of scope.** Scrimmage guards code that uses public APIs. Code that rewrites the private parts of Python's objects, such as a queue's `_buffer` or `_writer`, or Scrimmage's own state, can get around a gate; see [Security](security.md#who-scrimmage-defends-against).

- **Subinterpreters are refused in a run.** `InterpreterPoolExecutor` and `concurrent.interpreters` run code with no Scrimmage in it, so each is refused under a run.

- **Ray and Dask are not covered.** A task a run sends to a Ray or Dask cluster goes through their own network connections, which no gate sees, so it can reach a live write there. Dask's local threaded scheduler is threads, and follows the run.

- **A child on another venv's SDK is not version-checked.** The version rule of a service test covers the SDK in the service's own processes: the probe's report and each claim. A function or a script whose child runs on another venv with a different `scrimmage` is not compared, so install the same version in every venv your agent starts.

### Tools and frameworks

- **Tools, web requests and OpenAI model calls.** Scrimmage gates the tools you list under `tools`, the LangChain tools it finds by itself, requests sent with httpx or httpx2, and model calls. See [Safety](safety.md).

- **asyncio only for async agents.** An async model call outside asyncio, such as under trio, is refused and ends its run, because Scrimmage cannot budget it. Synchronous agents are not affected.

- **One framework.** LangChain tools, which LangGraph runs, have an adapter. A tool on another framework is not gated, so offering it to the model stops the test.

- **A tool's function called directly.** `my_tool.func(...)` or `my_tool.coroutine(...)` skips the gate, so nothing refuses or records it.

- **Tool names with no `tools` list.** They are checked after one baseline run, not before it, so a typo costs that one run. A tool the agent makes only in some runs may be reported as unknown.

- **An `_arun` that starts `_run` on the default executor itself** counts as two calls and two record lines. LangChain's own `_arun` counts as one.

- **A tool changed after the last check.** Classes are checked when they are made, tools when they are made or copied and when they run through LangChain, and all of them before each model request. So a class whose `invoke` or `run` is assigned after that, between the model's reply and the call or with no model request at all, runs unchecked. A `_run` set on a tool in that gap is still refused.

- **Code written to get around the gate.** None of these is caught: `object.__setattr__` on a copy, a false `__module__` on a class, `BaseTool.invoke = f` patching the library itself, a `__getattribute__` that shows the check a fake `_run`, and a gated tool offered for its schema while a custom node calls a plain function. See [Safety](safety.md#what-the-gates-assume).

- **A tool made with `model_construct`.** Its class is checked, but its name is not known, so offering it to the model stops the test.

- **One unsafe tool blocks its whole name.** A second, gated tool of the same name stops the test too, even if only that one is offered.

- **A plumbing type imported under another name.** If `RunnableConfig as HiddenCfg` is imported only under `TYPE_CHECKING`, a `config: HiddenCfg` argument on a direct `_run` call is recorded, because the written name does not say what it is.

- **A tool with no readable source on Python 3.14.** A function made in the REPL or by `exec`, or kept as a `.pyc` only, has no source to read, so a `config` argument beside an annotation that cannot be resolved is recorded.

- **A wrapper whose `__wrapped__` was set by hand to another function.** On Python 3.14, beside an annotation that cannot be resolved, Scrimmage reads the names of the function `__wrapped__` points to, so a `config` argument can be recorded.

- **LangChain versions.** Tested with langchain-core 1.7. A newer one gets a notice; an older one gets none.

- **A library that changes its tool functions** stops the program. Faults are not dropped with refusals kept, because the error type is part of what the gate relies on. The same goes for `mcp`: a mock must never become a real call.

- **Tools reached by import path.** A tool your code stores in a list or dictionary the moment it is defined keeps the original function, so the gate cannot reach it. A marker for those comes later.

- **Methods stored as the class is made.** A class whose metaclass, decorator or class body stores its methods in a table, and calls them from the table, keeps the original functions, so the gate cannot reach them. A bound method stored before the module finished loading does the same. A method looked up on the instance or the class at the call, such as `getattr(toolkit, name)`, is gated.

- **An alias at the top of a module.** `fetch = get_order` in a module keeps the original function under `fetch`, so a call through `fetch` is not gated or recorded. List the alias too, with `{path, name}`. An alias in a class body, `fetch = get`, is gated with its method.

- **Two toolkits with the same method names.** Two classes that both have `get_order` need `{path, name}` to tell them apart, and then the label is the name you gave, not the one the model sees, so a model request offering `get_order` is not matched to either. Test each toolkit in its own `scrimmage.toml`.

- **Python only.** The SDK must run inside the agent's own Python program.

### MCP tools

- **mcp 2.x only, tested with 2.3.** A program that imports `mcp` 1.x stops with exit code 3, even if it never calls an MCP tool, because 1.x has no place for the gate. Install `mcp` 2.x in the test environment.

- **MCP through the official client or a client built on it, such as FastMCP's.** A client of your own that speaks the protocol by itself is not gated at the MCP gate. If it sends its own web requests, its POSTs meet the web gate. If it is built on `streamable_http_client` and writes its own JSON-RPC messages, they go out through the transport's marked requests, undecided and unrecorded. Over stdio nothing sees them.

- **The old SSE transport for MCP.** Its web requests are not marked, so its first POST is refused by the web gate and the run stops. Use streamable HTTP or stdio.

- **MCP tools under another name.** A tool the model sees under a name other than the server's, such as `docs_search`, is reported as a tool no gate sees, and the test stops.

- **A hook that posts to the MCP endpoint.** An event hook on the MCP client that sends to the endpoint itself while a request is being sent passes the web gate unseen.

- **Only MCP tool calls are gated.** `resources/read`, `prompts/get` and the rest pass, and cannot be mocked or failed. A server can act on any of them. `resources/read`, `prompts/get`, `completion/complete` and `resources/subscribe` each leave a `real` line. See [Safety](safety.md#what-an-mcp-tool-may-do).

- **Two MCP servers with one name.** Scrimmage cannot tell them apart, and `mcp:docs/search` names both. Two servers that give no name are the same case.

- **An MCP label that reads two ways.** Tool `b/c` on server `a` and tool `c` on server `a/b` share the label `mcp:a/b/c`. The test stops once both are known, and there is no way to name either one.

- **`mcp:search` before the second server is known.** The ambiguous-label check runs at each call, so a call made before the second server was listed or called goes by that label.

- **A mock on a closed MCP session still answers.** A mock or a fault sends nothing, so it never finds out that the connection has gone.

- **A server that pages its tool list.** Scrimmage reads only the first page. When that page has a next page and lacks the mocked tool, the mock answers even if the tool does not exist, so a misspelled label is not caught. See [MCP tools](mcp.md#mocks).

- **A tool its server answers but does not list.** A server that lists tools by permission but takes calls by name can answer a tool it never lists. Such a tool cannot be mocked or recorded, and the test stops with exit code 2. List the tool on that server, or run it for real under `real`. See [MCP tools](mcp.md#mocks).

### Web requests and recorded mocks

- **POSTs a library sends by itself.** Everything through httpx or httpx2 is gated, so these are refused too until listed: the SDK of another model provider, telemetry exporters, and credential requests such as an OAuth token POST, including one an MCP client sends, even to the MCP server's own host.

- **Two HTTP libraries.** The gate sits in the clients of httpx and httpx2. A request sent with requests, urllib, aiohttp or a raw socket, or handed to a transport object by your own code, is not seen, so nothing refuses or records it. Only the `tools` list guards such a write: a listed tool that is neither mocked nor under `real` never runs. The web gates stop accidental writes, not code written to get around them. See [Safety](safety.md#what-the-gates-assume).

- **A form the model never sent while recording still misses.** `scrimmage record --runs N` makes a miss rarer, not impossible: at N = 5 a form sent in a quarter of the runs is never seen about a quarter of the time. See [Recording more than one run](recording.md#recording-more-than-one-run).

- **One web mock answers any query, at any N.** Several runs with the same method and path and equal replies give one mock that answers every query and every body.

- **The cross-run web rule leaves out `outside_tool` and `credential` calls.** A request made outside a tool in one run does not block the same request saved inside a tool in another.

- **A reply that changes with time is not written at N above 1.** `--runs 5` can write fewer mocks than `--runs 1` for a tool whose reply holds the time. Record it with `--runs 1`, write its mock by hand, or leave it under `real`.

- **Mocks with `args` match arguments exactly.** A model that sends `2.0` for `2`, adds an optional argument, or sends a list in another order misses the mock. No looser form exists: no subset, no ignored key, no pattern. The miss is reported with the arguments sent and the nearest mock, as a warning when the tool is under `real` by name; otherwise the call is refused, as before. The evidence is cleaned by a heuristic, and a secret that fits no pattern under an ordinary key name may print. A mock with no `args` answers every call to its tool; see [Mocks that miss](configuration.md#mocks-that-miss).

- **Request bodies that are not in memory.** A web request sent from a generator or as a multipart upload is decided as usual, but its body is recorded as 0 bytes.

- **A library that loses what mocks are built from.** The test stops with exit code 3 and names the library. Until it stops, a mocked read runs for real and a mocked write is refused, which errs on the safe side.

- **`scrimmage record` runs one baseline.** A call a later run would make is not seen; a recorded tool mock answers only the arguments it saw, so a new argument is a miss; see the exact-match entry above.

- **A web mock cannot match a query or a body.** Two GETs that differ only by query, or POSTs with different bodies, get `differs` and are listed by hand when their replies differ.

- **A value that changes between runs is frozen by its mock.** That is what a mock is for. A value that changes within one run is listed `nondeterministic`.

### Faults and model calls

- **A provider that is not OpenAI.** Listing its host under `real` lets the calls through, but they are web requests, so the budget does not see them. Name a host that speaks OpenAI's format under `model_hosts` instead.

- **One provider.** OpenAI's Chat Completions, Responses and embeddings, and gateways that speak the same format. Other providers come later.

- **A few faults.** `timeout`, `error` and `empty` on tools, `timeout`, `status N`, `refused`, `reset`, `dns` and `bad body` on model calls and web requests, `context too long` on a chat model call, `empty` on web requests, `timeout`, `error` and `empty` on MCP tools, `append`, `replace` and `slow` on tools, MCP tools and web requests, and `replace`, `slow`, `refuse`, `unknown tool`, `garble`, `truncate` and `bad tool args` on a chat model call.
  `append` on a model call comes later.

- **`empty` needs a declared type with an empty value.** On a tool declared to return a model, a dataclass, a number, `bytes`, a `Sequence`, a `Mapping`, a union of two kinds such as `str | list[str]`, or a type that cannot be resolved when it runs, or a LangChain tool with `response_format = "content_and_artifact"`, `empty` is a mistake in the test, and the tool never runs. Mock an empty result instead. See [Faults](concepts.md#faults).

- **Connection faults on a tool or an MCP tool.** `refused`, `reset` and `dns` work where Scrimmage sees the connection, on a model call or a web request. On a tool, write `do = "error"` with `raises = "ConnectionRefusedError"`, `"ConnectionResetError"` or `"OSError"`.

- **An injected `reset` never reaches the server.** The gate raises it before the request is sent. A reset after the server has read the request and done the work, with the reply lost, which tests duplicate writes on retry, is a later fault.

- **A connection fault's cause chain, in detail.** The chain has a real failure's shape: the library's error is raised from httpcore's, which holds the `OSError` as its hidden context, and on the async path anyio's link sits between them. Two details differ. On an async refused connection the `ConnectionRefusedError` at the bottom reads `Connection refused`, where a real one says `Connect call failed` and the address. On an async reset, anyio's `BrokenResourceError` lacks the `IndexError` a real one holds behind its cause. A client on a transport of its own, which never loads httpcore, gets the `OSError` as the direct cause.

- **The `dns` message is Linux's.** The gate writes glibc's text, `[Errno -2] Name or service not known`. On macOS a real failed lookup reads differently; the class, `socket.gaierror`, and the errno, `socket.EAI_NONAME` on that platform, still match.

- **A transport's own retries.** An httpx transport built with `retries=N`, such as `httpx.HTTPTransport(retries=2)`, retries a real failed connect below the gate, so it never sees an injected one. Under `when = "always"` the result is the same; under `first 1` a real refused connection can recover inside the transport, while the injected one reaches your code. Scrimmage does not detect the setting.

- **Only text and plain data take `append` and `replace`.** A result that is a model object, a dataclass, a number, `None`, `bytes` or a `str` subclass is not changed: the call is left as it is, the line says why, and a row with no place for the change is a mistake in the test. A dict, list or tuple takes only `replace` with `find`, on string values. See [Changing a result](changing-results.md#what-each-kind-of-call-can-take).

- **An MCP result with several text items.** `append` goes on the last one, and an agent that reads only `content[0].text` never sees it. With structured content, `replace` with `find` must change both the text and the structured content to count.

- **Web bodies that are not text or JSON.** A reply that is a stream (`text/event-stream`, NDJSON), has another content type, is encoded with `zstd`, or is over 10 MB is not changed. A digest header, such as `Content-MD5`, no longer matches a changed body. Clients other than httpx and httpx2, such as `requests`, `aiohttp` and `urllib`, are not seen, so the fault never fires.
  A model reply encoded with `zstd` is not changed either, and its cost is estimated from its size.
  A `br` reply is read with `brotli` or `brotlicffi` 1.2 or later from your program's environment; httpx and httpx2 ask for `br` only when one of them is installed.
  Without one, or with an older one, a `br` reply is not changed, the line says why, and a model reply's cost is estimated from its size.
  `brotlipy`, which also installs as `brotli`, counts as none: it cannot stop at a size, so Scrimmage leaves `br` alone while httpx still unpacks it for your program.
  A packed reply is unpacked to 10 MB at most for the record, so a larger one's `bytes` on the line shows that cap, not its full size.

- **`slow` outside asyncio.** A `slow` fault on an async tool or web request under trio needs an asyncio loop to wait on, so the call is not slowed and the line says so. An MCP call waits with anyio, which works on both.

- **A sync caller that gives up leaves its thread behind.** Under `slow`, a caller that stops waiting with `future.result(timeout=...)` does not stop the thread that waits. That thread runs the real call after the delay, so a caller that retries runs a listed real write twice. Python cannot stop another thread safely.

- **`slow` is not a late reply after the work.** The delay comes before the call. A call that does its work and replies late, which tests duplicate writes on retry, is a later fault. Nor is the delay drawn from a range.

- **The plan holds `find` as written.** The plan is how the program applies a fault, so a `find` cannot be redacted there or it would no longer match. It is held in memory by the runner and served to the program over the loopback control server, and is not written to the records folder; the results and the record line show `find` redacted. It comes from your own `scrimmage.toml`.

- **`${` in an injected text.** `${NAME}` is filled in from the environment anywhere in the file, `text` and `find` included, so an injection that needs a literal `${`, such as a Log4Shell-shaped text, writes it as `$${`.

- **A change over text the record removes is not made.** A `find` that touches a key, or an `append` that joins a key to the text before it, leaves the call unchanged and the line names the rule.

- **`replace` on a model reply is for Chat Completions text.** A streamed reply and a Responses API call are a mistake in the test, a reply with no text is left as it is, and a moderation or embedding result cannot take it.
  The reply is written back as compact JSON, so its bytes differ from the provider's even where its fields do not.

- **`bad body` on a streamed model request.** It is a mistake in the test: the call fails at once and the fault is not counted as fired. A stream cut midway is a later fault. Fault a request that is not streamed, or use `timeout` or a status on this one.

- **`context too long` is OpenAI's Chat Completions reply.** On a Responses call it is a mistake in the test: the call fails at once and the fault is not counted as fired. Other providers' replies come with those providers.
  The message names the numbers of the real reply it was saved from, 128000 and 140008 tokens, not your model's context or your prompt's length.

- **The faults in a model's reply are not for a streamed request.** `refuse`, `unknown tool`, `garble`, `truncate` and `bad tool args` on a streamed request are a mistake in the test: the call fails at once and nothing is sent. A changed stream is a later fault.

- **`unknown tool` is for Chat Completions.** On a Responses API call it is a mistake in the test, and nothing is sent: no source shows a model call a tool no one offered there.

- **A Responses agent that chains to a faulted reply is not supported.** A request whose `previous_response_id`, or an `item_reference` in its `input`, names a reply or an item a fault changed or built is a mistake in the test, and it is not sent: OpenAI holds nothing under those made-up ids. Send the conversation in `input`, as the OpenAI Agents SDK does by default.
  A reply fault on a request that names a `conversation` is a mistake in the test too, and nothing is sent: OpenAI would keep the real reply in that conversation, or none.

- **`truncate` on Responses keeps the real counts.** The reply's `usage.output_tokens` is what the model was billed for, not the request's `max_output_tokens`. With no text, the last `function_call` item is cut, as a real reply cut inside a call is.

- **A 400 after a reply fault on Responses is an infrastructure error.** OpenAI takes back a Responses message or function call under an id it never made and reads it as sent, as two real requests showed, so no 400 is known to follow a fault there, and none is marked `after_reply_fault`.

- **A built Responses reply's other fields are fixed.** After `refuse`, the reply repeats the fields your request set, such as `instructions`, `store`, `temperature`, `tool_choice` and `text`. Its `tools` is empty whatever your request sent, and `model` is the name your request sent.

- **A built reply's counts are an estimate.** `refuse` and `unknown tool` send nothing, so the reply's `usage` holds a rough count of the request and 10 output tokens, and its `system_fingerprint` is the one of the real reply it was made from. The line costs 0.
  Its `model` is the name your request sent, such as `gpt-4o-mini`, where a real reply names the snapshot, such as `gpt-4o-mini-2024-07-18`.
  It has one choice, whatever `n` your request asks for.

- **A changed reply keeps the real counts.** After `garble`, `truncate` or `bad tool args`, the reply's `usage` is what the model was billed for, so the line's cost is true. A cut reply's `completion_tokens` is then more than its text holds, and an agent that compares it with its own `max_completion_tokens` sees a reply that was not cut by the limit.
  The changed reply is written back as compact JSON, so its bytes differ from the provider's even where its fields do not.

- **`truncate` on a very short text.** A text of one character cannot be cut shorter, so the agent gets the same text with `finish_reason` `length`, and the fault counts as fired.

- **`garble` is one form of corrupted output.** It puts the stray token of one documented incident in every fifth word. Scrambled letters, other scripts and broken code are not built. A text in a script with no spaces between words is one word, so it changes once.

- **`bad tool args` breaks the JSON only.** Arguments that parse but break the tool's schema, such as a missing required field, are not built.

- **The Structured Outputs refusal is the guide's example.** No real refusal was captured: the `refusal` text and form come from OpenAI's Structured Outputs guide, and the plain one from a forum report.

- **`unknown tool` sends one form.** It is the call to `multi_tool_use.parallel` OpenAI's models have sent. A tool name you pick is not built.
  Its arguments name your first tool with `"parameters": {}`, so an agent that unwraps the call runs that tool with no arguments.
  It fires where the model picks its own tool: not with `tool_choice` `"none"`, a `tool_choice` that names a tool, or tools in the old `functions` field only; those calls are left as they are. It still fires with `parallel_tool_calls` false, as no source shows what a model does then.
  If your agent sends that call back, OpenAI's real 400 on the next request is judged as the run's outcome, and its line is marked `after_reply_fault`; see [The 400 after a reply fault](model-faults.md#the-400-after-a-reply-fault).

- **A body cut by a dropped connection.** `bad body` sends a whole reply whose JSON does not parse. A reply cut short because the connection dropped is a connection error, not a body to parse: the openai library raises `APIConnectionError` from httpx2's `RemoteProtocolError`, after its retries. Scrimmage does not inject that failure.

- **`requests N` needs a library that marks its retries.** It counts a retry by the openai library's `x-stainless-retry-count` header.
  A client that sends none, such as NeMo Guardrails' own, makes each attempt a request; the run then gets a mistake that says to use `first N` or `always`; `call N` counts attempts too, so it works there.

- **A model's path on a host under `real`.** A POST to `/chat/completions` on a host listed under `real` as a web request runs unbudgeted and cannot be faulted by model name; the run table notes it.
  A web API whose path ends the same way gets the note too, and still runs.

- **Guardrails.** Two rails on one model are faulted together, one model name on two hosts cannot be faulted apart, a fallback guard under an attack needs two faults in one run, and a guard called through `requests`, `aiohttp` or `urllib` is not gated.
  See [Testing your guardrails](guardrails.md#what-is-not-covered).

- **NeMo Guardrails' usage statistics.** NeMo sends them through `urllib`, which Scrimmage does not gate, unless `NEMO_GUARDRAILS_NO_USAGE_STATS=1` is set.

- **Two calls to one model.** A fault's `model` tells models apart, not callers. A planner and a main model that both use `gpt-4.1` are hit together, and `first N` counts them together. Give them different names through a gateway alias if you need to fail one. See [Failing one model](model-faults.md#failing-one-model).

- **A model name is compared whole.** No prefix, wildcard or family of dated names: `gpt-4o-mini` does not name `gpt-4o-mini-2024-07-18`. If no call sent the exact name, the fault never fires, so the test stops with exit code 2 and lists the names the calls used. If your code sends both `gpt-4o-mini` and `gpt-4o-mini-2024-07-18`, a fault on the first fires on it alone, the dated call runs for real, the row can pass, and nothing warns. Write the name your code sends, not the name a reply or a dashboard shows.

- **A fault with `model` needs a name it can read.** A call its label names is refused and stops the test if its request body is sent from a generator, is multipart or compressed, or has no `model` field, such as a Responses call on a stored prompt. For a stored prompt, send `model` on that call: the openai library takes it beside `prompt`, but what the provider does when the stored prompt names another model is not confirmed. Taking `model` off the fault hits the router again, and with a budget on any model call with no name, such as a Responses call on a stored prompt, is refused for having no price, so give it one as `""` under `[agent.prices]`, or set `max_cost_usd = "none"`. See [Failing one model](model-faults.md#a-request-with-no-name-scrimmage-can-read).

- **Two `model` keys in one body.** The last value wins when Scrimmage reads it, and a provider might choose differently.

### The budget

- **Prices need checking.** The built-in price table is small; check it against your provider's pricing, and use `[agent.prices]` for anything else.

- **One price per kind of token.** A price has no long-context tier, so models priced higher above 272K input tokens, such as `gpt-5.4`, `gpt-5.5` and the `gpt-5.6` models, are not in the built-in table. Under `[agent.prices]`, give such a model its long-context price if a call can go over 272K input tokens, or a long call costs more than the record says.

- **A hold that cannot end while a call waits.** A call that its own run's calls would keep out, even if every other run finished, waits at most 30 seconds, or half the time its run has left. A stream the agent keeps open across a second call, or one it stopped reading without closing, keeps its hold, so the second call ends in a "not reserved" stop. A hold in another run is waited for until near this run's `run_timeout`.

- **A killed runner takes its runs with it, but not their children.** On Linux each run is asked to be killed with SIGKILL when the runner dies, even if it ignores SIGTERM. A run's own child processes are not covered, and on other systems nothing is set, so a run can outlive a runner killed there.

- **A killed run's holds stay.** A run killed with SIGKILL, such as one that ignores SIGTERM, can leave a hold that never settles, as Scrimmage cannot tell whether its call was sent. It counts as spent for the rest of the test, so a call it keeps out is refused at once, and the report warns about it.

- **The first in line waits for room.** A waiting call is served in turn: it goes ahead only when it fits along with every call ahead of it, so a large call at the head keeps a smaller one behind it waiting, even when the smaller one would fit alone. A call far back in a long line asks again only every 1 to 2 seconds, so when room frees for many calls at once it can go ahead up to 2 seconds late; with a short `run_timeout` that is a real share of the run.

- **A waiter keeps its place until it stops waiting.** A waiting call asks again as often as the runner says, from a quarter of a second at the head of the line to 2 seconds far back, and its place in line does not depend on how fast it asks, so a slow or loaded machine does not reorder the line. Its place ends when it is granted, when it gives up or is interrupted, or when its run ends, even by SIGKILL. A waiter that stops asking while its run goes on keeps its place for 60 seconds, then loses it, so a lost hand-back cannot block the line for good. Three cases can leave such a dead place: a child process killed while it waits, a process paused in a debugger, and an event loop blocked by sync code for 60 seconds. Until it goes, the calls behind it wait, and some are refused rather than delayed: a call kept out by its own run's holds waits at most 30 seconds, and a call kept out by another run's waits until a tenth of its own `run_timeout`, at most 10 seconds, is left, so with a `run_timeout` under about 70 seconds it can give up before the dead place goes.

- **Cached input on OpenAI only.** Cached tokens are read from OpenAI's usage fields. A gateway that reports them another way, or a price under `[agent.prices]` with no `cached_input`, prices every input token at the full rate, so the cost is high, never low.

- **Cache writes on Chat Completions are unconfirmed.** OpenAI states for Responses that written tokens are part of the input count and apart from cached ones. Scrimmage reads Chat Completions' `prompt_tokens_details.cache_write_tokens` the same way, as the openai library names it, but no real Chat Completions reply with writes has confirmed it.

### Judging an answer

- **Rules are plain text.** `expect` and `forbid` find text in the answer, with no word boundaries, numbers or meaning. "shipped" is found in "has not shipped", and Markdown stays in the answer. See [Judging an answer](judging.md).

- **No model judges an answer, and there is no built-in list for "admits the failure".** You write the words.

- **A rule cannot tell where a word came from.** An agent that answers from general knowledge can pass a rule that its tools should have earned. That is why a fault that fires on every call must have its own rules, and even those are only as good as the words you pick.

- **A rate describes the runs that happened.** A few runs say little, the range shows how little, and neither is a promise about the next run.

- **One `runs` for every row.** A row cannot run more times than another.

### Records and secrets

- **A Vault secret read at a nested mount.** Scrimmage knows Vault's paths at the top of the API (`/v1/secret/`, `/v1/*/data/*` and `/v1/*/creds/*`), any request that carries `X-Vault-Token`, and any reply in Vault's envelope (`request_id`, `lease_id`, `renewable`, `lease_duration`). A mount under a namespace read with a Bearer token or through a proxy that adds the token itself, whose reply a proxy has also unwrapped, on a path like `/v1/team/kv/data/db`, looks like an ordinary API path, so `scrimmage record` can write its reply as a mock, with only the secret patterns and the key names removed. A two-segment path rule would treat `/v1/stations/KSEA/data/latest` as a secret too.

- **A live session cookie under `session_id` is kept.** The key-name rule treats `session_id`, like every name about a secret, as not a secret, so a value that is really a session cookie goes into the recorded file. Only the secret patterns and the customer-data warning stand between such a value and git.

- **A form field is cut at the first space, quote or `&`.** `password=hunter2` and a quoted `password='two words'` are removed, but an unquoted `password=two words` leaves ` words`, and `password: hunter2` with a colon is not matched.
  Any `NAME_` prefix is matched, as in `DB_PASSWORD=`, so a harmless field that ends in one of these words and is not on the exempt list, such as `my_token=5`, is removed too; a call that holds one cannot be written as a mock. The exempt names are on the security page.
  `None`, `True` and `False` alone as a value are taken for code and kept, and so is a bare name as an argument of a call that has a name, as in `f(password=changeme)`, right after the parenthesis or after `, `.
  After a bare comma it is not read as code, so `f(token=tok,secret=sec)` loses `secret=sec)`: a removal is the safe side.
  A parenthesis with no name before it, as in `set it (PASSWORD=hunter2) then`, is not a call.
  `user=admin, password=changeme, host=db` outside a call, and `password=Summer.Time`, are removed.
  Other names glued to an ASCII letter or digit are kept: `PGPASSWORD`, `PGPASS`, `MYSQL_PWD`, `authtoken` and `accesstoken` are removed by name, but `mytoken=5`, `dbpassword=x` and similar are not.
  A raw quote after a field name that sits inside a quote, as in a JSON fragment cut out of its string, does not open a value.
  `Authorization=Basic%20...`, a double-encoded `password%253D`, and a multipart body with `name="password"` on one line and the value on another are not cleaned.
  JSON inside other text, as in `Result: {...}`, or across NDJSON lines, is not read as JSON: it goes through the patterns, which cut at a quote, and the result can come out broken. Only a text that is JSON as a whole is kept valid.
  A JSON text that is redacted is written back with the input's spacing where it can be told, but pretty-printed JSON comes back on one line, and a `\u` escape style follows whether the input held any `\u`, so a text may be written with different escapes. Numbers are kept as written.
  Two keys that redact to the same text are kept apart by a suffix, in JSON text and in recorded mocks and tool values: the second becomes `[secret removed] 2`.
  A backslash followed by `n` or `r` ends a form value, since it is a line break in escaped text; any other backslash, as in `PGPASSWORD=p\$ss` or `password=hun\ter2`, is part of the value.
  An escaped quote with more of the value after it is part of the value, as in `password=abc\"2zz`; one before a space, a comma, a colon, a bracket or the end closes an escaped string and ends the value.
  In a string read out of a JSON text, and in a Python string, such as what a tool takes or returns or the agent's answer, a `\n` or `\r` with more of the value after it is a backslash and a letter, as in the path `password=C:\new2zz`, and is part of the value.
  There a raw quote with more of the value after it is part of the value too, unless the field name sits inside a quote, which the quote then closes.
  A text that is one whole JSON string literal, as in `"password=C:\newhunt1"`, is read as JSON, so a `\n` in it is a line break and ends a value there: `ewhunt1` after it is kept.
  This holds for a Python string of that shape too, and redaction did the same before these rules.
  The same text outside JSON, where `\n` cannot be told from an escaped line break, still ends the value at `\n`: `password=C:\new2zz` as plain text keeps `\new2zz`.
  The paging and tokenizer prefixes (`next_`, `page_`, `sync_`, `cursor_`, `eos_`, `pad_` and the rest) are kept only before `token`: `SYNC_PASSWORD=` and `next_secret=` are removed.
  A kept value, such as a paging token, ends where a field name or a key shape begins in it, and the rest is read again: `next_page_token=ab,password=x` keeps `next_page_token=ab,` and removes the password.
  A field name begins there as it does anywhere else, after any character that is not an ASCII letter or digit, `_` or a backslash, so `next_page_token=a/token=b` loses `token=b`, and `next_page_token=a_token=b` is read as the field `a_token=b`, which is removed.
  A name also begins after a `\n`, `\r`, `\t` or `\uXXXX` escape, and after a URL-encoded comma, `&`, `;`, space or `|` (`%2C`, `%26`, `%3B`, `%20`, `%7C`, in either case), so `next_page_token=ab%2Cpassword=x` loses the password.
  An encoded letter, as in `%70assword=x`, is not read as a letter of a name: RFC 3986 says a letter is never percent-encoded, so no encoder writes one.
  A key shape such as `sk-`, `AKIA` or `ghp_` is removed anywhere in a kept value, even glued to a letter.
  In text that is not valid JSON, such as a log that cut a JSON body off, a raw `\uXXXX` escape is a break before a name, whatever it holds: `psql\u0020password=x` and `a=1\u0026token=x` lose the value.
  An escape inside a name, as in `p\u0061ssword=x`, is not read as a name there, since no JSON writer escapes an ASCII letter; in a text that is JSON as a whole the escape is undone first, and the name is found.
  An edit to a string that holds a removed secret inside a JSON text is refused as a whole, even beside the secret; the other strings of that JSON text stay open.
  Redaction is a heuristic on known shapes.

  *Checked by: `tests/test_redact.py::test_the_old_module_kept_each_new_secret_and_the_new_one_removes_it`, `tests/test_redact.py::test_prose_empty_values_and_paths_are_kept`, `tests/test_redact.py::test_a_kept_value_ends_where_a_field_or_a_key_shape_begins_in_it`*

- **A secret with no pattern under an innocent key is written.** `{"note": "pw is hunter2"}` has neither a known pattern nor a secret-like key, so it goes in; the customer-data warning says to check the mocks.

- **A recorded `${` is written as `$${`.** `scrimmage.toml` reads `${NAME}` from the environment, so a recorded value that holds `${` is written as the literal `$${`, and reads back as the value it was.

- **A request to a credential or secret-store endpoint runs for real in a test.** It is never written as a mock, so the test still reaches it, as the recording did.

- **A mock's value is capped at 64 KiB of TOML.** A larger value, a body, or arguments are listed `too_large` and written by hand.

- **A burst of tool calls with no pause while recording.** While recording, each call saves two values through a queue of 64 items, about 30 calls at once. A call waits up to 10 seconds for room. If the queue stays full, `scrimmage record` says a burst lost a saved value, exits 3, and writes nothing for that test. Make fewer calls at once in that test.

- **A credential request from a thread is not seen as a tool's.** A request a tool makes from a thread pool or a child process has no parent, so `scrimmage record` lists it `outside_tool` or `credential` on its own and cannot tell which tool it served. If that tool returns the token, a mock of the tool holds it.

- **An outage from a thread is frozen into the mock, with exit 0.** A 503 a tool's thread got is `outside_tool`, so nothing marks the tool as failing, and its mock answers with whatever the tool made of it.

- **A model call from a thread is frozen inside a tool mock, with exit 0.** A model call a tool makes from a thread has no parent until Scrimmage links threads, so nothing marks the tool as having reached the model, and its mock holds the model's reply.

- **`passphrase`, `pw` and other short names are not key-name secrets.** The key rule's words are `password`, `passwd`, `pwd`, `secret`, `token`, `auth`, `authorization`, `cookie`, `session`, `credential` and the `api key` family. A value under any other key is kept. `secret_id`, Vault's AppRole credential, is removed.

- **A value under 8 characters is not removed from the records.** It would match too much other text. Keep tokens and secrets longer than that.
