# Examples

Each example is a small, complete project: the agent's code, its `scrimmage.toml`, and what `scrimmage test` prints.
Several start from the [function agent](function-agent.md) and change one thing.

| Example | What it shows |
|---|---|
| [A function agent](function-agent.md) | The smallest project: a loop, two tools, a mock and an `error` fault |
| [An agent written with the anthropic library](anthropic-agent.md) | The function agent over `messages.create`, with a mock, a tool `error` and a `status 529` model row |
| [An agent behind a model gateway](gateway.md) | `model_hosts` for a LiteLLM or OpenRouter host, a fault on a gateway alias, and a reply that states its cost settling the budget |
| [Scrimmage in CI](ci.md) | A repository with a GitHub Actions workflow that runs `scrimmage check` and `scrimmage test --max-cost-usd 1`, and a failed row that fails the job |
| [Recording what your service answers](recording.md) | `scrimmage record` on a tool under `real`, the recorded file, and a test that runs from it without the service |
| [A budget that stops a looping agent](budget.md) | `max_cost_usd`, a price of your own, "budget reached" on a fault row that retries, and a model with no price that is refused |
| [A script and its child process](script-agent.md) | `command`, and a write in a child process that is refused, then mocked |
| [LangGraph tools](langgraph.md) | A graph with no `tools` list, a tool error that crashes it, and the fix |
| [MCP tools](mcp.md) | A tool on an MCP server over stdio, mocked, timed out and failed |
| [A web request inside a tool](web-request.md) | The tool runs for real, and the request inside it is mocked or gets a 503 |
| [Judging an answer](judging.md) | `expect`, `forbid` and `pass_rate`, a rule that is too narrow, and the fix |
| [A fault on one model](one-model.md) | A busy large model, a fallback to a small one, and `model` in a fault |
| [An agent inside a web service](service-agent.md) | The function agent behind a FastAPI endpoint under uvicorn, tested with the same mock and fault |
| [When the model asks for a tool your agent doesn't have](unknown-tool.md) | `unknown tool`: an MCP server that drops a tool, with a crash and the fix, and a tool result that names a tool, with a refused call and the fix |

## How these pages are checked

Every file and every output on these pages is taken from a run.
The repository's test suite reads each page, writes its files into a new folder, runs `scrimmage test`, and compares what it prints with the page, line by line.
Only the folder name and the run ids are allowed to differ.
That suite uses a fake model server with scripted replies, so it needs no API key.

With your own key, the answers will use different words, and the costs will differ a little.
