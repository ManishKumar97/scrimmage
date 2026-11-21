# Changelog

Releases are listed newest first, each with its version, its date and what changed.

## 0.1.0 (unreleased)

The first public release.

Install it with `pip install scrimmage`, and import it as `scrimmage`.
It needs Python 3.11 or later.

What it tests:

- An agent that is a Python function, with its own loop or on LangGraph, a Python script and the child processes it starts, or a web service.
- Its Python tools, the web requests inside them sent with httpx or httpx2, and tools on MCP servers.
- OpenAI model calls, and gateways that speak the same format.

What a test can do to each call:

- **real:** let it run, when the test allows it.
- **mock:** give a fixed answer, so the real call never happens.
- **fault:** fail it the way a real failure looks, such as a timeout or an HTTP 429.

What it checks:

- Rules on the agent's answer and on the calls it made, with a pass rate for each row.
- A spending limit on model calls for each test.
- Each call goes into a record on your machine, and `scrimmage show` reads it.

Scrimmage runs on your machine, in your CI or in your cluster, and sends nothing to its authors or to any third party; see [Security](security.md).
What it does not do yet is on the [Limits](limits.md) page.
