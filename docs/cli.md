# Commands

## faultline test

```bash
faultline test [path/to/faultline.toml]
```

Runs every test in the file, which is `faultline.toml` in the current folder by default.

Before the first run it checks the file and every label, so a typo such as `tool:get_ordr` costs nothing:
the test stops and lists the tools it knows.

It prints one table row per test row and what the model calls cost, writes each run's record to `faultline-runs/<time>/`, and exits with:

| Code | Meaning |
|---|---|
| 0 | Every row produced a result (answered, crashed and timed out are results) |
| 2 | A mistake in the test, a failed baseline, or a test the budget stopped |
| 3 | An infrastructure error, such as a model call that failed with nobody injecting it, or a library version Faultline cannot gate |

Each run's own output, everything your agent printed, is in `faultline-runs/<time>/<run>.log`.

Stopping `faultline test` (Ctrl-C, or a cancelled CI job) stops every run and every program it started, and exits 3.

## faultline-run

```bash
faultline-run <command> [args...]
```

Runs a command with Faultline's startup file first on Python's path.
`faultline test` uses it to start each run; you rarely run it yourself.
On its own, without a test running, it changes nothing: the startup file does nothing unless a test switched Faultline on.
