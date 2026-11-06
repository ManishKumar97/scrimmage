# Commands

## derail test

```bash
derail test [path/to/derail.toml]
```

Runs every test in the file, which is `derail.toml` in the current folder by default.

Before the first run it checks the file, and every label against `tools` when that list is set, so a typo such as `tool:get_ordr` costs nothing:
the test stops and lists the tools it knows.
With no `tools` list, a framework's tool names are known only when the agent runs, so one baseline run goes first, alone. If a mock or fault names a tool the agent does not have, the test stops there with exit 2 and lists the real tools.

It prints one table row per test row and what the model calls cost, writes each run's record to `derail-runs/<time>/`, and exits with:

| Code | Meaning |
|---|---|
| 0 | Every row produced a result (answered, crashed and timed out are results) |
| 2 | A mistake in the test, a failed baseline, or a test the budget stopped |
| 3 | An infrastructure error, such as a model call that failed with nobody injecting it, or a library version Derail cannot gate |

Each run's own output, everything your agent printed, is in `derail-runs/<time>/<run>.log`.

Stopping `derail test` (Ctrl-C, or a cancelled CI job) stops every run and every program it started, and exits 3.

## derail-run

```bash
derail-run <command> [args...]
```

Runs a command with Derail's startup file first on Python's path.
`derail test` uses it to start each run; you rarely run it yourself.
On its own, without a test running, it changes nothing: the startup file does nothing unless a test switched Derail on.
