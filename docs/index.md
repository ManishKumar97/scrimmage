# Derail

Derail tests what your AI agent does when the things it depends on go wrong.

An agent asks a model what to do, calls tools, and asks again until it has an answer.
In testing, every tool answers and nothing times out, so you never see how the agent behaves when a tool fails.
In production, a tool times out and a weak agent writes a confident, wrong answer anyway, with no error logged.

Derail makes those failures happen on purpose, in a test, without changing your agent's code.
For each call your agent makes, a test decides what it gets back:

- **real**: the call runs as normal, if it is safe to;
- **mock**: a fixed answer, so the real call never happens;
- **fault**: a failure that looks exactly like a real one, such as a timeout or a 429 from the model.

Every call is written to a record, so you can see what the agent was given and what it did next.
Each answer is judged by rules you write, so a build can fail when a row falls below the line you set.

Fault injection is an established practice; Derail brings it inside any Python agent.

## Start here

| Page | What it covers |
|---|---|
| [Install](install.md) | Getting Derail onto your machine |
| [Quickstart](quickstart.md) | Your first test, in five minutes |
| [Failing the model](model-faults.md) | Rate limits, timeouts, schedules and the budget |
| [Agents on a framework](frameworks.md) | LangGraph, with no tools list |
| [An agent run as a script](scripts.md) | A command, its answer, its child processes and how it stops |
| [Web requests](web-requests.md) | Mocking and failing the request inside a tool |
| [MCP tools](mcp.md) | Mocking and failing a tool on an MCP server |
| [Judging an answer](judging.md) | `expect`, `forbid`, `pass_rate` and the result of a row |
| [Concepts](concepts.md) | Rows, runs, labels, mocks, faults, records |
| [Configuration](configuration.md) | Every setting in `derail.toml` |
| [Commands](cli.md) | `derail test` and `derail-run` |
| [How it works](how-it-works.md) | How Derail gets inside your agent |
| [Safety](safety.md) | Why a test never changes real data by accident |
| [Limits](limits.md) | What this version does not do yet |

## Status

Derail is at an early stage.
This version tests an agent that is a Python function, with its own loop or on LangGraph, or a Python script and the child processes it starts. It fails or mocks the agent's own Python tools, the web requests inside them and MCP tools, fails OpenAI model calls, keeps a budget, and passes or fails each row.
Other providers and more frameworks come in later versions.
