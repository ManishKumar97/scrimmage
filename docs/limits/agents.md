# Limits: how your agent runs

Limits of the function, script, web service, Docker and Kubernetes shapes, and of writing and checking a test.

## Writing and checking a test

- **`scrimmage check` does not import your code.** It finds a tool name that is not in `tools`, but not a misspelled import path such as `acme.tools:get_ordr`; the first run of `scrimmage test` finds that.
  A static lookup would call a tool made by assignment or a star import a mistake, and then `check` would fail a file `test` runs.

- **`scrimmage init --probe` can run calls a test baseline would refuse.** It runs the function you name up to the first model request that offers tools (passing up to 5 that offer none) with no tools list, so a plain tool function called before then, or code after a passed request, runs for real, and so does a write through a library Scrimmage does not gate, such as `requests`.
  Web writes through httpx, LangChain tools and MCP tools are still refused.
  A script is never probed.
  See [Drafting a test file](../init.md#what-the-probe-can-do).

- **The probe's placeholder keys are all or none.** With neither `OPENAI_API_KEY` nor `ANTHROPIC_API_KEY` set, the probe sets both.
  With one set, it sets neither, so an agent that builds an OpenAI client while only an Anthropic key is set fails at build time under the probe.
  Set the key of the client the agent builds.

- **A client pointed at another provider's own host is not seen by the probe.** Reading the code drafts a `model_hosts` line for it, such as an `OpenAI` client at `api.anthropic.com`.
  The probe does not see its request, because the gate treats a POST to a host another provider owns, on a path that provider does not know, as a plain web call.
  List the host under `model_hosts` by hand when the base URL is not written in the code.

- **`scrimmage init` reads the code, so it only guesses.** A tool made at run time, in a dict comprehension or by a framework has no mark the read can see, and the entry point is chosen by a heuristic.
  `--probe` finds tools by what the model is offered, at the first model request that offers tools: a tool offered after that is not seen.

- **The editor schema checks shape only.** `scrimmage schema` cannot see a tool name against `tools`, a type outside your packages or a program on your `PATH`.
  `scrimmage check` decides.

- **The mock type check reads the outer type and the first item.** A mock's value is checked against its tool's return annotation by its outer type, such as `list` or `dict`, and, when the items are a class, by its first item only.
  So `-> list[str]` with a number in the list gets no warning.
  Items are read only for `list[X]`, `tuple[X, ...]` and `dict[str, X]` themselves: the table items of `-> list[Order] | None` or `-> Sequence[Order]` get no warning.
  A wrong value is a warning, never a mistake, because the annotation can be the wrong one.

- **A module reached through a `__path__` its parent changes.** Scrimmage finds a listed module without importing its package, so a module that only a `__path__.append(...)` in the parent's `__init__.py`, or `pkgutil.extend_path`, makes importable is reported as not found.
  List a module the package's own folders hold.

- **`--json` prints at the end.** One document, when the command ends; nothing is printed while a test runs.

## A function or a script agent

- **`process:sh` lets any shell command through.** A start is decided by the program's name alone, so listing `process:sh` makes every `shell=True` call and every `os.system` real, whatever the command.

- **A command that clears Scrimmage's path.** `env PYTHONPATH=. python bot.py` replaces the startup folder, so the program runs with no gates, its writes are real, and only then does the run read infrastructure error.
  Add to `PYTHONPATH` in the environment of `scrimmage test` instead.
  A flag that does the same, `python -E`, is refused when the file is read.
  A project must not have its own module named `scrimmage` in a folder of `PYTHONPATH`, because it would be imported in place of Scrimmage.
  There are two outcomes.
  If the folder is only on the agent's path, `scrimmage check` and `scrimmage test` refuse it before any run, and name the file.
  If the folder is also on the `scrimmage` command's own path, as with an absolute `PYTHONPATH`, or `PYTHONPATH=.` run from the project root, the command imports your file first and stops with `ModuleNotFoundError: No module named 'scrimmage.entry'; 'scrimmage' is not a package`, and your file has already run.
  The cure is the same: rename the module.

- **A blind child not started from Python.** A Python child with its own `env`, or with `-I`, `-E` or `-S`, is refused when your Python code starts it.
  Only those starts are seen, so `sh -c 'python x'`, `env -i python x`, `os.system`, and any exec from a program that is not Python, such as a shell script or a wrapper, are missed: the child runs with no gate and no line.
  A shell script that runs Python has the same gap if it clears the environment.

- **A blind child on another Python.** A Python that is not the run's, with its own `env`, passes the check and is not kept from a listed tool.
  That includes a venv elsewhere made from the same binary.
  The base binary of your venv counts as this Python, and is refused.

- **multiprocessing after an edit of the environment.** `spawn` and `forkserver` start their workers through a call no audit event reports.
  A worker inherits `os.environ`, so it joins, unless your code removed `PYTHONPATH` or `SCRIMMAGE_TOKEN` from it first.
  Then it runs blind, with no refusal.

- **Refusals that can surprise you.** Each is a `process:python` refusal, fixed by the message:
  `pip install` of a source package on the run's Python, as pip builds it under a `PYTHONPATH` of its own;
  `env={**os.environ, "PYTHONPATH": "..."}`, which replaces Scrimmage's folder, so add to the value instead;
  a tool on the run's Python with a shebang such as `#!/usr/bin/python3 -Es`, as on a system venv.

- **A tool in a file another program runs.** `python -m cProfile bot.py` and `runpy.run_path("bot.py")` run `bot.py` as `__main__` with a program Scrimmage does not read as `bot.py`, so a listed tool defined in it is not stopped and writes for real, exit 0.
  Keep tools in a module.

- **A wrapper that is a Python program with no Scrimmage.** `poetry run python bot.py` stops with exit code 3, as poetry is the program under test.
  Use a shell script: see [An agent run as a script](../scripts.md#the-command).

- **What a stdio server does by itself.** A server on the library's default environment does not join.
  The MCP gate decides every call to it, but a write the server makes is not gated.

- **Python is told by name or shebang.** A program that embeds Python under another name is not seen as Python.

- **The first Python with the SDK owns the run.** In a wrapper, another venv with Scrimmage installed that runs first takes the run, and your bot, on a different Python, is then a program on another Python: only listed tools are kept from it.
  A step on the same Python before the bot makes the bot a child, so no fault fires in it.
  With a wrapper and no SDK, a Python that is really your agent runs ungated until the run reads infrastructure error, with listed tools kept out.

- **A root under `uv run` that execs itself.** It comes back as a child, so it fires no faults.
  A root that scrimmage-run became directly stays the root.

- **Programs on another Python.** They are not gated, so a web write or an MCP call from one is not stopped or recorded.
  Only a tool under `tools` is kept from them, by module name and file ending.
  A copy of its code is not.
  With `children` set, one that has Scrimmage installed joins, and a stdio server on `mcp` 1.x then stops with exit code 3.

- **A daemon that starts its own session.** A child that calls `os.setsid`, or is started with `start_new_session=True`, leaves the run's process group, so the group stop does not reach it.
  A sweep of `/proc` after the run finds it by the exact `SCRIMMAGE_RUN` in its environment, kills it and records it.
  Until then it can run.
  A process that starts with a clean environment holds no such variable and is not found.
  A system with no `/proc`, such as macOS, has no sweep: the daemon keeps running, a 409 on its next line refuses its later calls while the test goes on, and a `real` call after `scrimmage test` has ended runs for real.
  A double fork with no new session stays in the group and is stopped.

- **A last line of output shared with a child.** The last printed line is read from one file, so a child that prints after your program comes last, and nothing says so.
  Print the answer last or use the answer file.
  See [An agent run as a script](../scripts.md#the-answer).

- **`first N` in children.** With `children` set each process counts its own calls, so `first 1` fires once per process, and `call 2` on the second call of each.

- **A child's gates wait for the control server.** A child has no sender thread, so each of its gated calls waits for its record line to be sent, and an async gate in a child holds up the event loop for that long.

- **`os.fork` warns on Python 3.12 and later.** The SDK's sender thread makes your program multi-threaded, so Python prints a `DeprecationWarning` at `os.fork` in your own code, and raises it under `-W error`.

- **Stopping needs Python to run.** A program stuck in native code that holds Python's lock cannot run the SIGTERM handler, so SIGKILL ends it at five seconds, and the lines of its last 100 ms are lost.
  The same goes for a program whose own handler does not exit.

- **SIGTERM taken by a thread that is not Scrimmage's.** Linux gives a signal sent to a process to any thread that does not block it, and can pass over a main thread that is waiting.
  Scrimmage's own threads block SIGTERM and SIGINT, so it never goes to one of them.
  A thread your code or a library started, such as asyncio's worker threads, can still take it.
  If your main thread is then waiting on a call that never ends, the handler does not run until that call returns, so SIGKILL ends the program at five seconds and the call has no line.
  Block the two signals in your own threads with `signal.pthread_sigmask` to avoid it, but not in a thread that starts a program, which would start with them blocked too.

- **A hung call under your own SIGTERM handler.** Your handler replaces the SDK's, so a call going on when it runs gets a line only if the handler exits normally through Python's exit steps.
  If it ends with `os._exit` or the signal, or never returns, the call has no line.

- **A root that execs another program.** Its lines are sent first, and the program's last printed line can be the answer.
  If that program is not Python, nothing after the exec is gated or recorded.
  If it never prints, the run reads as crashed.

- **The startup file needs Python 3.8 or later.** On an older Python 3, a child's startup prints an error.
  Only 3.11 to 3.14 are run in tests; 3.8 syntax is checked by parsing.

- **A call with no plan on no server.** If the control server cannot be reached, a child's refusal has nowhere to go: the call is still refused, and the run ends as usual, with no line.

## An agent inside a web service

- **A service run is stricter than a script about processes.** A script's child joins its run.
  In a service, a run is one request in a process that serves others, so every process start under a run is refused unless `real` lists its program as `process:<program>`, and so are the forks, `multiprocessing` starts and sends, process pools and subinterpreters that [Safety](../safety.md#an-agent-inside-a-web-service) lists by name.
  A Python child never joins a service run.

- **Signals are not gated.** `os.kill`, `os.killpg` and `signal.pthread_kill` reach another process with no decision: a run can stop a process, or make one start again.
  A relaunch of Python's resource tracker after a run killed it is refused like any other start; see [Safety](../safety.md#an-agent-inside-a-web-service).

- **A run's changes to its process carry over.** The working folder, `os.environ`, signal handlers and every other object the service process shares belong to the whole process, so a run that changes them changes them for later plain requests, for work outside any request and for any process started after it, a relaunch of Python's resource tracker included.
  That covers an item waiting in a queue's buffer, which a run can change before the feeder pickles it, and pickler reducers a run registers with `ForkingPickler.register` or `copyreg`, which then run for every later pickling.

- **Other ways to another process are not gated.** Shared memory (`multiprocessing.Value`, `Array`, `shared_memory`), a lock or a semaphore shared with another process, a socket or a file another process reads, and any send point not on that list reach another process with no decision and no line.

- **A queue's private parts are not gated.** Code that takes a `multiprocessing` queue's lock or semaphore, or empties its buffer, can stall the queue for every request, and for the processes that share it, but sends them nothing.
  Code that puts back the pickler a queue's feeder uses removes the feeder's check: its sends are then decided as any other send, so one made while no run is open passes.

- **A process pool's `close()`, `terminate()` or `shutdown()` inside a run is refused.** Each sends to the pool's processes.
  The refusal comes before the stop changes anything, so the pool still works.
  Stop pools at the service's shutdown, outside any request.

- **`--loop asyncio` on Python 3.11.** On 3.11 Scrimmage watches each task a run makes through the loop's `create_task`, which uvloop's loop does not let it do.
  A service on uvloop stops at its start with a message naming `--loop asyncio`.
  From 3.12, uvloop works.

- **On Python 3.11, a task built outside `create_task` is not seen at a run's close.** A run whose task outlives it is an infrastructure error.
  On 3.11 that check sees tasks made with `asyncio.create_task` or `loop.create_task` only; from 3.12 it reads every task on every loop.

- **Each control request is a new connection.** The service opens a TCP connection and, over TLS, a TLS handshake for every claim, record batch and budget ask, and a closed connection sits in the host's connection-tracking table for 120 seconds.
  With the usual table of 262,144 entries it fills at about 2,200 new connections a second; the kernel then drops new connections without a reply, and runs end as `plan-unavailable` infrastructure errors.
  Watch `control_p99_ms` and the report's `control server:` lines.
  Keeping connections alive is a later step.

- **A control path cut mid-test does not stop the test.** When a NetworkPolicy or a firewall drops the agent's traffic to the runner after the probe, every later run is still sent, and each waits up to 10 seconds for its claim before its gate answers `503 plan-unavailable`.
  Every run after the cut is an infrastructure error and the test ends with exit 3, but a long test takes about 10 seconds a run to get there.

- **A clock jump during a test can show as a lost connection.** A service whose clock moves more than 60 seconds from the runner's after the probe has every signed request refused.
  A claim says so, and its run names the clock, but a record batch over 4 MB is refused before its body is read, so the service sees a closed connection and the run's record is incomplete.

- **One runner per service at a time.** Two tests against one service, each with its own control server, are not detected.
  Start one test at a time.

- **Work outside any request has a cost while a test runs.** While a run is open, a background job, a lazy worker or a thread a plain request left running cannot be told from a run's own work.
  So its writes are refused unless `background_real` lists them, and each such call, a read included, ends every run open at that moment as an infrastructure error.
  A team whose service does background work while tests run lists that work under `background_real`.

- **A long plain request keeps the threads it started plain until it ends.** A thread a request without a run starts, such as a worker a streaming reply or a WebSocket starts and feeds, runs as that request while it is open, so work a run hands to that thread runs for real.
  Once the request ends, the same work meets the outside rules.

- **gunicorn is not covered, with `UvicornWorker` either.** Its workers are forks of one process, and a fork has none of Scrimmage's threads, so each such worker stops at its first request and says so.
  Run the app with `uvicorn --workers N`.

- **A service row's runs go one after another.** Each run of a row is sent once the row's run before it is settled, after its grace and wait, so a refusal stops its row before the next run.
  Other rows run beside it, so `parallel` is spent across rows, not within one.

- **A call `background_real` lets through is decided once, with what it runs inside.** Outside any request, a `multiprocessing` send, pool task or subinterpreter call listed as `process:multiprocessing` is decided when it is made.
  What runs inside it is not decided again: the pickling of its arguments (an object's own `__reduce__`), the unpickling of a pool's results and of a subinterpreter's error, a `context=` or `Process` factory it is given, and a pool's `initializer`, run again when a worker is replaced, as `maxtasksperchild` does.
  A program started or a send made there has no line.
  Under a run each such call is refused before any of it runs.

- **A start made below multiprocessing's own calls is not seen before Python 3.14.** `_posixsubprocess.fork_exec` called directly, or a forkserver's `Process._Popen` called by hand, raises no audit event and passes no gate before 3.14.
  `Process.start`, `subprocess`, `os` and `multiprocessing.util.spawnv_passfds` are all decided.
  Code written to get around the gate is not caught; see [Safety](../safety.md#what-the-gates-assume).

- **The setup request lists only what it saw.** Outside work that did not happen while the setup request was open is not in its list, and surfaces later as an infrastructure error naming the entry to add.

- **A lazy worker started inside a run.** A thread a run starts and keeps for later requests, such as a batcher, holds that run after it ends: its calls are refused, and the next run claimed in that process is an infrastructure error that names the thread.
  Start such workers at startup, outside any request.

- **The Starlette gate alone lets an outer middleware see the run header.** Under uvicorn the gate is outermost.
  Without uvicorn's gate, an ASGI middleware wrapped around the Starlette app, outside it, sees `x-scrimmage-run` before the gate removes it.

- **hypercorn, `--http zttp` and `--http2` are not covered.** zttp and HTTP/2 stop the service at its start. hypercorn is not tested.

- **Stopping the service mid-run.** uvicorn installs its own SIGTERM handler, so runs open when it stops lose their last lines, and read as infrastructure errors.

- **A killed runner leaves the copy's workers on macOS.** On Linux, the program `start` names and uvicorn's workers are killed when the process that started them dies, so a runner killed with SIGKILL takes a uvicorn copy with it. macOS has no such call: there the workers keep serving the port until you stop them.

- **A killed runner leaves the copy's other programs.** On Linux, a runner killed with SIGKILL takes only the program `start` names and the workers of a uvicorn started directly by it.
  A program that is not Python, such as `sleep 600 &` in a shell wrapper, a uvicorn under such a wrapper, and a helper your app starts, keep running until you stop them.
  Scrimmage does not ask them to die with the copy: Linux would kill such a program when the thread that started it ends, and a helper started from a short-lived thread must keep running.
  The end of a test, Ctrl-C and SIGTERM stop every program in the copy's process group.

- **A killed runner leaves its token folder.** A runner killed with SIGKILL cannot delete the `scrimmage-token-` folder it made in the temporary folder.
  The token in it opens nothing once that runner is gone, and the system clears the folder with the rest of its temporary files.

- **Another process can take the port before the copy binds it.** Scrimmage picks a free port, and the copy binds it a moment later.
  If another process takes it in between, the copy exits and is started again on a new port, up to 3 starts.
  After the third, the test stops with exit 3, the ports it tried and the copy's last lines.

- **A copy's bind error that names no address can blame the wrong port.** Where the log's "address in use" line has no address, as with `uvicorn --workers` or hypercorn, Scrimmage asks `/proc` instead.
  Any socket on the picked port counts, an outgoing connection's local port too, because it blocks a bind as a listener does.
  A copy that fails on a port its command fixes can then be started again, up to 3 starts; the last message shows the copy's own log lines.

- **Without `/proc`, a copy that answers with no gate is named only after 30 seconds.** On a system with no `/proc`, such as macOS, Scrimmage cannot tell whether the copy or another process answered the probe.
  So it waits up to the probe's 30 seconds for the copy to exit, as one that lost its port does once it has loaded the app.
  On Linux it names a copy with no gate at once.

- **A process that leaves the copy's group with `setsid` is not stopped.** With `start`, Scrimmage stops the copy of the service by its process group, so a process the service starts in a session of its own keeps running after the test.

- **A service run refuses every process start, a library's own too.** The openai library runs `uname -p` through `platform` to name its machine, so a service whose agent calls OpenAI needs `"process:uname"` under `real`.
  The setup request finds it before any run and names the entry to add.

- **A service run that queues 10,000 record lines it cannot send waits.** Each run keeps up to 10,000 lines while the runner is away.
  Once full, a refusal or a model call waits for room in the request that made it, so that request slows until the runner answers again; other lines are dropped and counted.

## In Docker Compose

Also read the control-path limits under An agent inside a web service.

- **A client without the token can hold the control server's connections.** When `control` is not loopback, any container or pod on the host can open the runner's 64 connections and send nothing.
  Each is cut after 15 seconds, and while all are held the runs end as `plan-unavailable` infrastructure errors, never as results.
  A network policy that limits who may connect is the fix.

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

- **The default certificate lasts one day.** A runner that starts after it expires exits 2, and a test that outlives it has new handshakes refused; make a new one as [When the certificate expires](../kubernetes.md#when-the-certificate-expires) shows.

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

## Programs and processes

- **Three agent shapes.** The agent is a Python function, a Python script, or an ASGI web service under uvicorn, such as a FastAPI app.
  See [An agent inside a web service](../services.md).

- **Code that rewrites private internals is out of scope.** Scrimmage guards code that uses public APIs.
  Code that rewrites the private parts of Python's objects, such as a queue's `_buffer` or `_writer`, or Scrimmage's own state, can get around a gate; see [Security](../security.md#who-scrimmage-defends-against).

- **Subinterpreters are refused in a run.** `InterpreterPoolExecutor` and `concurrent.interpreters` run code with no Scrimmage in it, so each is refused under a run.

- **Ray and Dask are not covered.** A task a run sends to a Ray or Dask cluster goes through their own network connections, which no gate sees, so it can reach a live write there.
  Dask's local threaded scheduler is threads, and follows the run.

- **A child on another venv's SDK is not version-checked.** The version rule of a service test covers the SDK in the service's own processes: the probe's report and each claim.
  A function or a script whose child runs on another venv with a different `scrimmage` is not compared, so install the same version in every venv your agent starts.
