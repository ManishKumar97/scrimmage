# Commands

`python -m scrimmage` is the same command as `scrimmage`, on the Python that runs it.

## scrimmage test

```bash
scrimmage test [path/to/scrimmage.toml] [--compare EARLIER] [--max-cost-usd USD] [--records DIR] [--json]
```

Runs every test in the file, which is `scrimmage.toml` in the current folder by default.
With `--json` it prints one JSON document in place of the table; see [JSON output](json-output.md).
`--max-cost-usd USD` lowers the file's `max_cost_usd` to `USD` for this test, a number of dollars above 0.
It never raises it: the lower of the two is the cap, and a file with `max_cost_usd = "none"` gets `USD`.
The report and the document's `budget_usd` name the cap used.
A test that finished writes `result.json` into its records folder, so `scrimmage show` can read it later.
A test stopped with Ctrl-C writes one too, for the runs that had ended, marked stopped.
`--records DIR` puts the records folder inside `DIR`, in place of `scrimmage-runs/` beside the file.
Use it when the file's own folder cannot be written, such as a file mounted from a Kubernetes ConfigMap.
A `DIR` that is missing is made, mode `0700`, with a `.gitignore` inside it.
Only `DIR` itself gets that mode: any folder made above it has the default mode.
A `DIR` that cannot be made, such as one under a read-only folder, is exit 2 and the message names it.
A relative `DIR` is relative to the folder you run the command in, not to the file.
A `DIR` that already exists is used as it is: its mode is not changed and no `.gitignore` is written, so `--records .` never alters the folder it names.
It must be yours and no wider than `0700`.
One that others can enter is exit 2; run `chmod 700` on it, or point `--records` at a new folder inside it.
One owned by another user is exit 2 as well, even when it is group-writable, as a volume's mount point is; point `--records` at a new folder inside it, such as `DIR/scrimmage-runs`.
`scrimmage show DIR/<time>` reads the folder it makes.
With `--compare EARLIER` it prints, after its own output, how each row changed from an earlier test; see [Reading a finished test](show.md).

Before the first run it checks the file, and every label against `tools` when that list is set, so a typo such as `tool:get_ordr` costs nothing:
the test stops and lists the tools it knows.
With no `tools` list, a framework's tool names are known only when the agent runs, so one baseline run goes first, alone. If a mock or fault names a tool the agent does not have, the test stops there with exit 2 and lists the real tools.

It prints one table row per test row with its pass rate and result, then which rules failed and what the model calls cost. See [Judging an answer](judging.md). It writes each run's record to `scrimmage-runs/<time>/` beside the file, or inside `--records DIR`, and exits with:

| Code | Meaning |
|---|---|
| 0 | Every row passed |
| 1 | A row failed |
| 2 | A mistake in the test, a failed baseline, a row with no result, or a test the budget stopped |
| 3 | An infrastructure error, such as a model call that failed with nobody injecting it, or a library version Scrimmage cannot gate |

The higher code wins: a failed row in a test with a mistake exits 2. The table still shows every row.

Each run's own output, everything your agent printed, is in `scrimmage-runs/<time>/<run>.log`.
For a script, what it printed to standard output is in `<run>.out` and its standard error in `<run>.log`.

Stopping `scrimmage test` (Ctrl-C, or a cancelled CI job) stops every run and every program it started, and exits 3.

## scrimmage check

```bash
scrimmage check [path/to/scrimmage.toml] [--json] [--runs N] [--max-cost-usd USD] [--service]
```

Checks the file the way `scrimmage test` does before its first run, and runs nothing.
`--runs N` counts the runs `scrimmage record --runs N` would start, one baseline per test, N times, and checks `N` as `record` does.
With `--max-cost-usd USD` too, it checks `N` under that cap, as `record --runs N --max-cost-usd USD` would run; `budget_usd` in its summary stays the file's own cap.
`scrimmage check` reads the file only: it starts no program, no control server and no model call, and makes no `scrimmage-runs/` folder, so it costs nothing.
It catches every bad key, value and label, a tool name that is not in `tools`, a mock's `type` outside your packages, a `command` it cannot find, a flag that would skip Scrimmage's startup file, a fault word the row's own provider can never take, such as `bad tool args` on `model:anthropic`, and a row no answer can pass.
A file `scrimmage check` passes is one `scrimmage test` starts.
`--service` is the one form that reaches the network; see below.
For a [service](services.md), it also prints how the service is started.
With `start`, that is the command as the file writes it, after `the service starts with:`.
Without it, it is the two variables to start the service with.

It does not import your code. A misspelled import path, such as `acme.tools:get_ordr`, is found when the first run imports the module, as in `scrimmage test`.

```
$ scrimmage check
scrimmage check: scrimmage.toml is valid: 2 tests, 5 rows, 15 runs, 4 tools, a budget of $5, OpenAI prices as of 2026-11-22, Anthropic prices as of 2026-11-25
```

The budget line ends with the date of the prices the budget is measured at.
When you price models yourself or have run `scrimmage prices update`, more lines name each price's source, the file and the date of each provider's section of it.

Each problem goes to standard error, one line each, and the last line to standard output:

```
$ scrimmage check
scrimmage: [[test]] 'order-status': tool:get_ordr names no tool in [agent] tools; the tools are get_order, check_inventory
scrimmage check: 1 problem; nothing was run
```

| Code | Meaning |
|---|---|
| 0 | The file is valid |
| 2 | A mistake in the file, or a file that cannot be read |
| 3 | Scrimmage itself failed while reading the file |

### scrimmage check --service

```bash
scrimmage check scrimmage.toml --service [--json]
```

After the file passes, `--service` checks the [service](services.md) the file names, the way `scrimmage test` does before its first run, and stops there.
It reads the token file, starts the control server for the length of the command, and sends the probe.
With `start` in the file, it reads no token file: it starts its own copy of the service, as `scrimmage test` does, probes it, and stops it before it exits.
A copy that fails to start prints its last lines; its `service.log` goes with the command's scratch folder.
While the service's ping to the control server times out, is refused or cannot resolve the name, the probe is asked again for up to 30 seconds, so the command can take that long before it names the failure.
It sends no run and no setup request, makes no model call and leaves no `scrimmage-runs/` folder, so it costs nothing.
Without `--service`, `scrimmage check` reads the file only, as above.

```
$ scrimmage check --service
scrimmage check: scrimmage.toml is valid: 1 test, 2 rows, 2 runs, 4 tools, no budget
service     POST http://127.0.0.1:8765/chat   gated (asgi, starlette), pid 7
version     scrimmage 0.1.0rc3 in the service and here
python      3.12.9 (main, Oct  9 2026, 00:00:00) [GCC 12.2.0] at /usr/local/bin/python
tools       check_inventory, get_order, get_stock, refund_order   (match the file)
control     http://127.0.0.1:8766 reached, no TLS, on this machine, this runner, 3 ms
notices     none
```

Each line is judged on its own, so a problem on one line hides none of the others.
The exit code is that of the first problem, in the order of the lines.

| Code | Meaning |
|---|---|
| 0 | Every line is good. A warning, such as a notice the service holds, still exits 0 |
| 2 | A mistake to fix: the tools the service gates are not the file's, or the service's SDK is another version |
| 3 | The service is not gated, cannot reach the control server, or its clock and this machine's differ by more than 60 seconds; with `start`, the copy exited or did not answer in 30 seconds |

`--json` adds a `service` object to the `check` document; see [JSON output](json-output.md#scrimmage-check-json).

## scrimmage prices update

```bash
scrimmage prices update [path/to/scrimmage.toml]
```

Fetches the newest `prices.toml` from the repository's `main`, over HTTPS, and saves it as `scrimmage-runs/prices.toml` beside the file.
It is the only time Scrimmage reads from the network, and it runs only when you type it; see [Security](security.md).
It checks the file against the schema first, prints what changed from your last copy, or from the shipped prices if there is none, then writes it.
The diff is grouped by provider, with the dates of the old and the new section at the head of each group.
Each group lists each model added or removed, and each changed rate, old to new.
A provider's section that only the new file has is listed as added, whole, and one that only your copy has as removed.
A file that fails its checks is exit 2, and a fetch that fails is exit 3.
In both nothing is written.
The folder is made mode `0700` with a `.gitignore` inside it, as `scrimmage test` makes it.
See [Where prices come from](configuration.md#where-prices-come-from).

## scrimmage record

```bash
scrimmage record [path/to/scrimmage.toml] [--test NAME]... [--refresh LABEL]... [--runs N] [--json] [--max-cost-usd USD]
```

Runs each test's baseline, once or `--runs N` times, with its mocks and nothing injected, and writes each real tool, MCP and web call as a mock in `scrimmage.recorded.toml`, beside your file.
Your file is never changed. See [Recording replies](recording.md).
`--test NAME` records only that test. `--refresh LABEL` records a label again that already has a mock.
`--runs N` runs each test's baseline N times, 1 by default, and writes one exact mock for each set of arguments the runs sent.
`N` is a whole number from 1 to 1000, and the file's `runs` is not read. See [Recording more than one run](recording.md#recording-more-than-one-run).
`--max-cost-usd USD` lowers the cost cap as it does for `scrimmage test`.

| Code | Meaning |
|---|---|
| 0 | Every call that needs a mock got one, and every run answered |
| 1 | The file is written, and a call is listed by hand, or a run did not answer |
| 2 | A mistake: the file, `--runs`, a refused call, an unseen tool, the budget, or a file layout record cannot extend. A stop on the budget writes nothing |
| 3 | An infrastructure error, including a bug in Scrimmage while it recorded; nothing is written for a test whose record is incomplete |

## scrimmage schema

```bash
scrimmage schema > scrimmage.schema.json
```

Prints the JSON Schema of `scrimmage.toml` that ships with your installed Scrimmage, and exits 0.
An editor uses it to check the file as you type; see [Checking the file in your editor](configuration.md#checking-the-file-in-your-editor).
The schema checks the shape of the file. `scrimmage check` decides.

## scrimmage cert

```bash
scrimmage cert --ip 172.17.0.1 --name host.docker.internal --out .scrimmage-cert [--days 30]
```

Makes the certificates for a control server that a service in a container reaches over TLS; see [A service in a container](docker.md#a-service-in-a-container).
It writes three files into a new folder, mode 0700, and refuses a folder that exists:

| File | What | Mode |
|---|---|---|
| `ca.pem` | The authority's certificate. The service names it in `SCRIMMAGE_CONTROL_CA`. | 0644 |
| `server.pem` | The control server's certificate, with each `--name` as a DNS name and each `--ip` as an IP address. | 0644 |
| `server.key` | Its private key. | 0600 |

The authority's key exists only while the command runs and is discarded once the server certificate is signed, so no file can sign another name.
The authority is limited to the names and addresses you give and their subdomains.
Given no `--ip`, it excludes every IP address, so it vouches for names alone.
Given `--ip` with no `--name`, DNS names are not limited, so give both, as the documented setups do.
Give at least one `--ip` or `--name`; both can be repeated.
`--days` is the validity, 1 to 397, and 30 by default.
Making new certificates means running the command again, then restarting the runner and the service.

It prints the two lines the service's environment needs, and the `control_tls` line for the file, with absolute paths so the line works from any folder.
In a container, `SCRIMMAGE_CONTROL_CA` is the path where `ca.pem` is mounted there, not the printed path.
The port is left as `<port>` for you to fill in.
The command needs the `cert` extra, `pip install "./packages/scrimmage[cert]"` from the clone; without it the command says so and exits 2.

| Code | Meaning |
|---|---|
| 0 | The three files were written |
| 2 | No name or address, a name or address a certificate cannot carry, a folder that exists, a folder that cannot be written, or the extra is not installed |

## scrimmage path

```bash
scrimmage path
```

Prints one line: the absolute path of the folder `scrimmage-run` puts first on `PYTHONPATH`.
That folder holds one file, Scrimmage's startup file.
The command reads no file and no environment variable, needs no token, and exits 0.
It prints nothing else, so `PYTHONPATH=$(scrimmage path)` works in a shell.

A start line you cannot change, such as a Kubernetes manifest, sets that path itself; see [An agent inside a web service](services.md).

It exits 2 when the folder is not a real folder on disk, as in a zipped install, and says to install `scrimmage` from its wheel.

## scrimmage show

```bash
scrimmage show [TARGET] [--run RUN] [--compare EARLIER] [--json]
```

Reads a finished test's records folder and says what happened: the table, and for each failing row the timeline of its first failing run and one suggestion, labelled a heuristic.
`TARGET` is a records folder or its `result.json`, and the newest test's folder by default.
`--run RUN` shows one run.
`--compare EARLIER` compares the folder with an earlier test and runs nothing.
It makes no model call, writes nothing, and opens no socket. See [Reading a finished test](show.md).

| Code | Meaning |
|---|---|
| 0 | Something was shown |
| 2 | No such folder or run, a bad run id, or a file `show` refuses to open |
| 3 | Scrimmage's own failure |

## scrimmage init

```bash
scrimmage init [folder] [--out PATH] [--probe MODULE:FUNCTION] [--json]
```

Writes a first `scrimmage.toml` for the agent in the folder, the current folder by default.
It reads the code with `ast`, imports nothing and runs nothing, unless you pass `--probe`.
The draft has no real tools, no test and no mock, and `--out`, or `scrimmage.toml` in the folder, is never replaced.
The entry point in the draft is a guess, and a guess is never run. Neither output gives a ready command for it: choose the function, and name it with `--probe module:function`.
`--probe module:function` runs only the function you name, up to its first model request, which is refused before it is sent, and reads the tools that request offers; a bare `--probe`, or a value that is not `module:function`, exits 2 and runs nothing.
The probe costs $0 in model calls Scrimmage sees; a paid call through a library it does not gate is not seen. `--json` never turns the probe on. See [Drafting a test file](init.md).

| Code | Meaning |
|---|---|
| 0 | A draft was written with an entry point |
| 2 | No entry point was found, or the target file exists |
| 3 | Scrimmage's own failure, or the probe's infrastructure error |

## --version

```text
scrimmage --version
```

It prints `scrimmage` and the installed version on one line, and exits 0.
The runner and the SDK in a service must run the same version, so this is the line to compare.

## --json

`scrimmage test --json`, `scrimmage check --json`, `scrimmage record --json` and `scrimmage show --json` print exactly one JSON document on standard output and nothing else.
Standard error stays empty unless Python itself fails.
The document's `exit` field is always the process's exit code.
Once Scrimmage has started, Ctrl-C or SIGTERM still prints the document, with exit 3.
Scrimmage has started from the moment its entry point runs, a few tens of milliseconds after Python does; a signal before that, while Python itself starts, ends the process with no document.
A signal after the command's result is settled is ignored, so the exit code always equals the document's `exit`.
Called from Python, signal handling needs the main thread; from another thread, a signal does not stop the run, so wait for it.
A mistake in the command line itself, such as an unknown flag, is still text on standard error, with exit 2.
`scrimmage init --json` prints its one document the same way.
[JSON output](json-output.md) describes every field.

## scrimmage-run

```bash
scrimmage-run <command> [args...]
```

Runs a command with Scrimmage's startup file first on Python's path, then becomes that command, so it keeps its arguments, its process id and its exit code.
`scrimmage test` starts every run through it, and you can put it in front of a command yourself, in a script or a container's start line.
On its own, without a test running, it changes nothing: the startup file does nothing unless a test switched Scrimmage on.

A web service is started through it once, with `SCRIMMAGE_SERVICE=1` and the token file in its environment, such as `scrimmage-run uvicorn shop.service:app --port 8765 --loop asyncio`.
Each process that loads the app prints `scrimmage: active in service mode (pid N)`; a service with a mistake in that environment stops before it listens and says why.
See [An agent inside a web service](services.md).

`scrimmage-run --help` or `-h`, as its first argument, prints the usage line and exits 0.
After the command's name, `--help` belongs to the command.

If the command cannot start, `scrimmage-run` says why on standard error and exits with:

| Code | Meaning |
|---|---|
| 2 | No command was given |
| 126 | The command was found but cannot be run |
| 127 | The command was not found |

## scrimmage-mcp

```bash
scrimmage-mcp --config PATH [--root DIR] [--max-session-usd USD] [--allow-no-budget] [--allow-probe] [--call-timeout SECONDS]
```

The Scrimmage MCP server, installed with the `mcp` extra.
Without the extra it prints how to install it and exits 2.
It lets a coding agent run `check`, `test`, `record`, `init` and `show` as tools, over stdio, with a cost cap on every spend.
See [Driving Scrimmage from a coding agent](mcp-server.md).
