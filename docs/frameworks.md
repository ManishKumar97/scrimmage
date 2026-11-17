# Agents on a framework

If your agent uses a framework Scrimmage has an adapter for, you write no `tools` list and change no code.
Scrimmage finds each tool when your code creates it.

This version has one adapter: LangChain tools, which LangGraph runs.

## A LangGraph agent

```python
# package/graph.py
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

...


@tool
def get_order(order_id: str) -> str:
    """Look up an order's status."""
    ...


def answer(question: str) -> dict:
    return graph.invoke({"messages": [("user", question)]})
```

```toml
[agent]
function = "package.graph:answer"
answer   = "messages[-1].content"
real     = ["tool:check_inventory"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped"]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call = "tool:get_order"
do   = "error"
expect = [["sorry", "unable"]]
forbid = ["shipped"]
```

A graph returns its state, not text, so `answer` says where the answer is in what your function returned.
A path can use field names, dots and indexes.
It must lead to the answer, as text or a number. A path to a list or a table is a test mistake, because tool results in it could pass a rule.
A LangChain message can hold a list of content blocks, so use `messages[-1].text`, which joins them, rather than `messages[-1].content`.
If the path does not fit, the run ends as crashed, and its `.log` file says which step failed.

## Tool names

A label uses the name the model sees, which LangChain takes from the tool.
With no `tools` list, Scrimmage learns those names only when the agent runs, so it runs one baseline alone first:

- a mock or fault naming a tool the agent does not have stops the test there, with exit 2 and a list of the real tools, so a typo costs one run;
- at the end, a mock no run in any row used gets a note, because its label is correct and only the question did not need the tool; the exit code stays 0;
- if no run reported its tools, the labels went unchecked, so the test fails with exit 2.

A tool made only later in a run, after the first baseline ended, is not known to that check.

## What the error fault does

`do = "error"` raises `ToolException`, the type LangChain treats as a tool failure.
It is raised at the point your tool's own code would run, inside LangChain's error handling, so LangChain treats it as it treats a real failure:

| Your tool | What happens |
|---|---|
| Sets `handle_tool_error` | LangChain turns a `ToolException` into a tool message, and the model carries on |
| Builds `ToolNode(tools, handle_tool_errors=True)` | The tool node turns any error into a tool message, whatever its type |
| Does neither | The error ends the graph, and the run is marked "crashed" |

Both are real results. The second is what your agent does in production when a tool fails, unless you have added error handling.

To raise something else, name it:

```toml
[[test.fault]]
call   = "tool:get_order"
do     = "error"
raises = "ConnectionError"
expect = [["sorry", "unable"]]
forbid = ["shipped"]
```

`raises` is a Python built-in exception, or `package.module:Class` from your agent's own packages.
A tool's `handle_tool_error` handles only `ToolException`, so a `ConnectionError` ends the graph even with it set, as a real one would, unless the tool node handles every error.

A tool built with `response_format = "content_and_artifact"` returns a pair, so its mock is a list of two items: `returns = ["the content", {a = "b"}]`.

## Tools that cannot be gated

Scrimmage gates what a tool really runs, wherever it comes from: a decorator, a class, a mixin, or a `_run` assigned to the class later.
When Scrimmage finds that a call could skip the gate, the tool does not run: the test stops with exit 2, and the reason is in the output.
This guards against mistakes in ordinary code, not code written to get around it; see [Safety](safety.md).

- A `_run` that is not a plain function, such as a `staticmethod`, cannot hold a gate, so the call is refused and the test reports a mistake.
- A `_run` or `_arun` set on the tool itself, not on its class, is refused the same way, including one set in `model_post_init`.
- A class that overrides a function on LangChain's tool classes, such as `invoke`, `run`, `batch`, `stream` or `__call__`, may never reach the gate. The override is replaced by a refusal, when the class is made, so its code does not run, whether the model is offered the tool or your code calls it. Move the work into `_run` or `_arun`, where the gate sits.
- This goes by name, so a helper method that only shares a name with a library function, such as `transform`, `map` or `pick`, is refused too. Rename the helper.
- Overriding `_run`, `_arun`, `__init__`, `model_post_init` or `as_tool`, and adding fields or properties, is fine: those do not skip the gate.
- A class changed after it was made is checked again before each model request.
- Model requests carry only names, so one tool of a name that cannot be gated stops the test for every tool of that name.
- Calling `my_tool.func(...)` or `my_tool.coroutine(...)` yourself skips LangChain and the gate. Call the tool through `invoke`, or list the function under `tools`.

## Parallel tool calls

LangGraph can run several tool calls from one model reply at once.
They share one set of counters, so no two get the same number.
The order they arrive in can change between runs, so when a model may call a tool several times at once, `when = "always"` gives the same result every time.

## Other frameworks

The faults on model calls work under any framework, because they sit below it, in the HTTP library.
A tool on a framework with no adapter is not gated, so offering it to the model stops the test with a message naming it.
To gate such a tool, list its function under `tools`; see [the settings](configuration.md#agent).

## A tool that calls an MCP server

A LangChain tool that calls `session.call_tool` inside it meets two gates, the tool's first.

- With no entry for `tool:<name>`, the tool is refused, and the MCP call never happens.
- A mock on `tool:<name>` replaces the whole tool, so the MCP call never happens either.
- With `tool:<name>` under `real`, the tool's code runs, and its MCP call is decided as usual by its `mcp:` label.

See [MCP tools](mcp.md).
