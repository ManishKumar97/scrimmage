# Agents on a framework

If your agent uses a framework Faultline has an adapter for, you write no `tools` list and change no code.
Faultline finds each tool when your code creates it.

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
```

A graph returns its state, not text, so `answer` says where the answer is in what your function returned.
A path can use field names, dots and indexes.
If the path does not fit, the run ends as crashed, and its `.log` file says which step failed.

## Tool names

A label uses the name the model sees, which LangChain takes from the tool.
With no `tools` list, Faultline learns those names only when the agent runs, so it runs one baseline alone first:

- a mock or fault naming a tool the agent does not have stops the test there, with exit 2 and a list of the real tools, so a typo costs one run;
- at the end, a mock no run in any row used is a mistake in the test, so a mock on a tool the agent never calls does not pass silently.

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
```

`raises` is a Python built-in exception, or `package.module:Class` from your agent's own packages.
A tool's `handle_tool_error` handles only `ToolException`, so a `ConnectionError` ends the graph even with it set, as a real one would, unless the tool node handles every error.

A tool built with `response_format = "content_and_artifact"` returns a pair, so its mock is a list of two items: `returns = ["the content", {a = "b"}]`.

## Tools that cannot be gated

Faultline gates what a tool really runs, wherever it comes from: a decorator, a class, a mixin, or a `_run` assigned later.

- A `_run` that is not a plain function, such as a `staticmethod`, cannot hold a gate, so the call is refused and the test reports a mistake.
- A class that overrides `invoke`, `ainvoke`, `run` or `arun` may never reach the gate, so offering it to the model stops the test.
- Calling `my_tool.func(...)` yourself skips LangChain and the gate. Call the tool through `invoke`, or list the function under `tools`.

## Parallel tool calls

LangGraph can run several tool calls from one model reply at once.
They share one set of counters, so no two get the same number.
The order they arrive in can change between runs, so when a model may call a tool several times at once, `when = "always"` gives the same result every time.

## Other frameworks

The faults on model calls work under any framework, because they sit below it, in the HTTP library.
A tool on a framework with no adapter is not gated, so offering it to the model stops the test with a message naming it.
