# Commands

`scrim` is a short name for `scrimmage`: `scrim test` and `scrimmage test` are the same command.

## scrimmage test

```bash
scrimmage test [path/to/scrimmage.toml] [--compare EARLIER] [--json]
```

Runs every test in the file, which is `scrimmage.toml` in the current folder by default.
With `--json` it prints one JSON document in place of the table; see [JSON output](json-output.md).
A test that finished writes `result.json` into its records folder, so `scrimmage show` can read it later.
With `--compare EARLIER` it prints, after its own output, how each row changed from an earlier test; see [Reading a finished test](show.md).

Before the first run it checks the file, and every label against `tools` when that list is set, so a typo such as `tool:get_ordr` costs nothing:
the test stops and lists the tools it knows.
With no `tools` list, a framework's tool names are known only when the agent runs, so one baseline run goes first, alone. If a mock or fault names a tool the agent does not have, the test stops there with exit 2 and lists the real tools.

It prints one table row per test row with its pass rate and result, then which rules failed and what the model calls cost. See [Judging an answer](judging.md). It writes each run's record to `scrimmage-runs/<time>/`, and exits with:

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
scrimmage check [path/to/scrimmage.toml] [--json]
```

Checks the file the way `scrimmage test` does before its first run, and runs nothing.
`scrimmage check` reads the file only: it starts no program, no control server and no model call, and makes no `scrimmage-runs/` folder, so it costs nothing.
It catches every bad key, value and label, a tool name that is not in `tools`, a mock's `type` outside your packages, a `command` it cannot find, a flag that would skip Scrimmage's startup file, and a row no answer can pass.
A file `scrimmage check` passes is one `scrimmage test` starts.

It does not import your code. A misspelled import path, such as `acme.tools:get_ordr`, is found when the first run imports the module, as in `scrimmage test`.

```
$ scrimmage check
scrimmage check: scrimmage.toml is valid: 2 tests, 5 rows, 15 runs, 4 tools, a budget of $5
```

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

## scrimmage record

```bash
scrimmage record [path/to/scrimmage.toml] [--test NAME]... [--refresh LABEL]... [--runs N] [--json]
```

Runs each test's baseline, once or `--runs N` times, with its mocks and nothing injected, and writes each real tool, MCP and web call as a mock in `scrimmage.recorded.toml`, beside your file.
Your file is never changed. See [Recording replies](recording.md).
`--test NAME` records only that test. `--refresh LABEL` records a label again that already has a mock.
`--runs N` runs each test's baseline N times, 1 by default, and writes one exact mock for each set of arguments the runs sent.
`N` is a whole number from 1 to 1000, and the file's `runs` is not read. See [Recording more than one run](recording.md#recording-more-than-one-run).

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

## scrimmage show

```bash
scrimmage show [TARGET] [--run RUN] [--compare EARLIER] [--json]
```

Reads a finished test's records folder and says what happened: the table, and for each failing row the timeline of its first failing run.
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

If the command cannot start, `scrimmage-run` says why on standard error and exits with:

| Code | Meaning |
|---|---|
| 2 | No command was given |
| 126 | The command was found but cannot be run |
| 127 | The command was not found |
