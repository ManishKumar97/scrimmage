# Commands

## derail test

```bash
derail test [path/to/derail.toml] [--json]
```

Runs every test in the file, which is `derail.toml` in the current folder by default.
With `--json` it prints one JSON document in place of the table; see [JSON output](json-output.md).

Before the first run it checks the file, and every label against `tools` when that list is set, so a typo such as `tool:get_ordr` costs nothing:
the test stops and lists the tools it knows.
With no `tools` list, a framework's tool names are known only when the agent runs, so one baseline run goes first, alone. If a mock or fault names a tool the agent does not have, the test stops there with exit 2 and lists the real tools.

It prints one table row per test row with its pass rate and result, then which rules failed and what the model calls cost. See [Judging an answer](judging.md). It writes each run's record to `derail-runs/<time>/`, and exits with:

| Code | Meaning |
|---|---|
| 0 | Every row passed |
| 1 | A row failed |
| 2 | A mistake in the test, a failed baseline, a row with no result, or a test the budget stopped |
| 3 | An infrastructure error, such as a model call that failed with nobody injecting it, or a library version Derail cannot gate |

The higher code wins: a failed row in a test with a mistake exits 2. The table still shows every row.

Each run's own output, everything your agent printed, is in `derail-runs/<time>/<run>.log`.
For a script, what it printed to standard output is in `<run>.out` and its standard error in `<run>.log`.

Stopping `derail test` (Ctrl-C, or a cancelled CI job) stops every run and every program it started, and exits 3.

## derail check

```bash
derail check [path/to/derail.toml] [--json]
```

Checks the file the way `derail test` does before its first run, and runs nothing.
`derail check` reads the file only: it starts no program, no control server and no model call, and makes no `derail-runs/` folder, so it costs nothing.
It catches every bad key, value and label, a tool name that is not in `tools`, a mock's `type` outside your packages, a `command` it cannot find, a flag that would skip Derail's startup file, and a row no answer can pass.
A file `derail check` passes is one `derail test` starts.

It does not import your code. A misspelled import path, such as `acme.tools:get_ordr`, is found when the first run imports the module, as in `derail test`.

```
$ derail check
derail check: derail.toml is valid: 2 tests, 5 rows, 15 runs, 4 tools, a budget of $5
```

Each problem goes to standard error, one line each, and the last line to standard output:

```
$ derail check
derail: [[test]] 'order-status': tool:get_ordr names no tool in [agent] tools; the tools are get_order, check_inventory
derail check: 1 problem; nothing was run
```

| Code | Meaning |
|---|---|
| 0 | The file is valid |
| 2 | A mistake in the file, or a file that cannot be read |
| 3 | Derail itself failed while reading the file |

## derail schema

```bash
derail schema > derail.schema.json
```

Prints the JSON Schema of `derail.toml` that ships with your installed Derail, and exits 0.
An editor uses it to check the file as you type; see [Checking the file in your editor](configuration.md#checking-the-file-in-your-editor).
The schema checks the shape of the file. `derail check` decides.

## --json

`derail test --json` and `derail check --json` print exactly one JSON document on standard output and nothing else.
Standard error stays empty unless Python itself fails.
The document's `exit` field is always the process's exit code.
Once Derail has started, Ctrl-C or SIGTERM still prints the document, with exit 3.
Derail has started from the moment its entry point runs, a few tens of milliseconds after Python does; a signal before that, while Python itself starts, ends the process with no document.
A signal after the command's result is settled is ignored, so the exit code always equals the document's `exit`.
A mistake in the command line itself, such as an unknown flag, is still text on standard error, with exit 2.
[JSON output](json-output.md) describes every field.

## derail-run

```bash
derail-run <command> [args...]
```

Runs a command with Derail's startup file first on Python's path, then becomes that command, so it keeps its arguments, its process id and its exit code.
`derail test` starts every run through it, and you can put it in front of a command yourself, in a script or a container's start line.
On its own, without a test running, it changes nothing: the startup file does nothing unless a test switched Derail on.

If the command cannot start, `derail-run` says why on standard error and exits with:

| Code | Meaning |
|---|---|
| 2 | No command was given |
| 126 | The command was found but cannot be run |
| 127 | The command was not found |
