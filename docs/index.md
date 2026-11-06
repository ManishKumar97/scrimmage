# Faultline

Faultline tests what your AI agent does when the things it depends on go wrong.

An agent asks a model what to do, calls tools, and asks again until it has an answer.
In testing, every tool answers and nothing times out, so you never see how the agent behaves when a tool fails.
In production, a tool times out and a weak agent writes a confident, wrong answer anyway, with no error logged.

Faultline makes those failures happen on purpose, in a test, without changing your agent's code.
For each call your agent makes, a test decides what it gets back:

- **real**: the call runs as normal, if it is safe to;
- **mock**: a fixed answer, so the real call never happens;
- **fault**: a failure that looks exactly like a real one, such as a timeout or a 429 from the model.

Every call is written to a record, so you can see what the agent was given and what it did next.

Fault injection is an established practice; Faultline brings it inside any Python agent.

## Start here

| Page | What it covers |
|---|---|
| [Install](install.md) | Getting Faultline onto your machine |
| [Quickstart](quickstart.md) | Your first test, in five minutes |
| [Failing the model](model-faults.md) | Rate limits, timeouts, schedules and the budget |
| [Agents on a framework](frameworks.md) | LangGraph, with no tools list |
| [Concepts](concepts.md) | Rows, runs, labels, mocks, faults, records |
| [Configuration](configuration.md) | Every setting in `faultline.toml` |
| [Commands](cli.md) | `faultline test` and `faultline-run` |
| [How it works](how-it-works.md) | How Faultline gets inside your agent |
| [Safety](safety.md) | Why a test never changes real data by accident |
| [Limits](limits.md) | What this version does not do yet |

## Status

Faultline is at an early stage.
This version tests an agent that is a Python function with its own loop. It fails or mocks the agent's own Python tools, fails OpenAI model calls, and keeps a budget.
Other providers, web requests, MCP tools and more frameworks come in later versions.
