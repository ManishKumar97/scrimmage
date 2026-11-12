# Driving Scrimmage from a coding agent

The Scrimmage MCP server lets a coding agent, such as Claude Code or Cursor, run Scrimmage through typed tools.
It is a different thing from [MCP tools your agent calls](mcp.md), which are about mocking and failing the MCP tools of the agent under test.

The server wraps five commands: `check`, `test`, `record`, `init` and `show`.
Each tool call runs the command as a child process, `python -P -m scrimmage_core <command> --json`, on the server's own Python, and returns its one JSON document.
It speaks MCP over stdio only: the client starts the server, and nothing listens on a socket.

## Install

Install the `mcp` extra in the project's environment, the same one that runs your agent:

```bash
pip install "scrimmage-core[mcp]"
```

`scrimmage test` starts your agent on its own Python, so the server must run on that Python too.
The extra pins `mcp` to 2.3.
If your agent pins `mcp` 1.x, installing the extra conflicts with it.

## Adding it to a client

For Claude Code, from the project folder:

```sh
claude mcp add scrimmage --scope project -- .venv/bin/scrimmage-mcp --config scrimmage.toml
```

That writes `.mcp.json`, which you can also write by hand:

```json
{
  "mcpServers": {
    "scrimmage": {
      "type": "stdio",
      "command": ".venv/bin/scrimmage-mcp",
      "args": ["--config", "scrimmage.toml", "--max-session-usd", "10"]
    }
  }
}
```

Other clients take the same command and arguments in their own file.
No client has been checked against this server yet except the SDK's own scripted client.

```
scrimmage-mcp --config PATH [--root DIR] [--max-session-usd USD] [--allow-no-budget]
              [--allow-probe] [--call-timeout SECONDS]
```

| Flag | Meaning |
|---|---|
| `--config PATH` | The `scrimmage.toml` every tool reads, relative to the root. It must be inside the root. It may not exist yet, so `init` can write it |
| `--root DIR` | The project folder, the working folder by default. It may not be `/` |
| `--max-session-usd USD` | The most this server's calls may spend in all, $10 by default |
| `--allow-no-budget` | Run `test` and `record` on a file with `max_cost_usd = "none"`; the call's `max_cost_usd` is then the only cap |
| `--allow-probe` | Let `init` take a `probe` |
| `--call-timeout SECONDS` | Stop a command that runs longer than this, 1800 by default |

## The tools and what each costs

| Tool | What it runs | Cost |
|---|---|---|
| `check` | `scrimmage check` | Nothing: it reads the file only |
| `show` | `scrimmage show`, with `target`, `run` and `compare` | Nothing: it reads a records folder only |
| `init` | `scrimmage init`, with `folder`, and `probe` only with `--allow-probe` | Nothing, unless a probe runs your code up to its first model request |
| `test` | `scrimmage test`, with `compare` | Your agent's model calls, up to `max_cost_usd` |
| `record` | `scrimmage record`, with `tests`, `refresh` and `runs` | Your agent's model calls, up to `max_cost_usd` |

`test` and `record` need a `max_cost_usd` argument, which you see in your client's approval prompt before the call spends anything.
The cap the command runs under is the lower of that argument and the file's `max_cost_usd`.
Approving a call to `test` approves whatever the config's entry point does now, with the code as it is at that moment; the `check` result names that entry point and the labels under `real`.

A result whose exit is 2 or 3 is an error, and carries its document.
Exit 1, a failed row or a record with calls left for you, is a result, not an error.
Each result starts with a short summary built from the document's fields, then the document as JSON text, and carries the document as structured content too.
Its `_meta` holds `scrimmage/guard`: the call's `max_cost_usd`, the file's cap, the entry point, the labels under `real`, and the session's spend and ceiling.

## The guards

Before `test` or `record` runs, the server:

1. refuses the call if the session's spend plus its `max_cost_usd` would pass `--max-session-usd`;
2. runs `scrimmage check`, and returns its document if the file has a mistake, running nothing;
3. refuses a file with `max_cost_usd = "none"`, unless the server started with `--allow-no-budget`;
4. refuses a file whose cap is above the call's `max_cost_usd`, so the larger number is one you approve.

One `test`, `record` or probe runs at a time; a second is refused at once, and `check` and `show` still run.
A call that was stopped counts at least its `max_cost_usd` against the session, and one with no document counts all of it.
The guards stop accidents, such as a loop or a forgotten cap; they are not a boundary against a coding agent with a shell, which can edit `scrimmage.toml` or run `scrimmage test` itself.

Allowlist `check` and `show` in your client, and never `test` or `record`, so each spend still asks you.

## What reaches the coding agent

A result handed to a coding agent goes to that agent's model provider.
The server names paths relative to the project folder and writes your home folder as `~`, and runs `redact` on every message.
`redact` removes known secret shapes, such as API keys; it does not remove customer data.
A `record` result's `mocks_text`, when present, carries the recorded responses of real tools word for word, after the recorder's own secret removal.
Standard error from a command is never forwarded; when a command prints no document, the result gives only its last line, redacted.

## Cancelling and stopping

When the client cancels a call, the server stops the command as `scrimmage test` stops on SIGTERM: every run is stopped and the command prints its document.
The SDK does not answer a cancelled call, so that document, its status and its records folder are in the server's log, on its standard error, which the client keeps.
A command still running 15 s after SIGTERM gets SIGKILL, and then every process that started with the call's mark in its environment is killed, so a run's own child processes are stopped too.
A signal to the server, SIGTERM, SIGHUP or SIGINT, sends SIGTERM to every command at once.
On a system with no `/proc`, such as macOS, there is no such sweep, and a stop relies on Scrimmage's own stop alone.
A server killed with SIGKILL cannot stop anything; each command's own cap and `run_timeout` still bound what is left.
