# LangGraph tools

LangGraph is LangChain's library for agents built as a graph of steps.
Teams use it to run tool-calling agents in production.
Scrimmage finds LangChain tools by itself when your code creates them, so this test has no `tools` list.

Libraries this example needs: `langgraph`, `langchain-core` and `langchain-openai`.
This page was checked with langgraph 1.2, langchain-core 1.8 and langchain-openai 1.8.

## The files

```python title="shop/graph.py"
"""An order bot in LangGraph: a model step and a tool step, in a loop."""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@tool
def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    raise RuntimeError("the orders system is not reachable from this machine")


@tool
def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    raise RuntimeError("a real refund must never run in a test")


TOOLS = [get_order, refund_order]
model = ChatOpenAI(model="gpt-4o-mini").bind_tools(TOOLS)


def think(state: State) -> dict[str, Any]:
    return {"messages": [model.invoke(state["messages"])]}


builder = StateGraph(State)
builder.add_node("think", think)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge(START, "think")
builder.add_conditional_edges("think", tools_condition, {"tools": "tools", END: END})
builder.add_edge("tools", "think")
graph = builder.compile()


def answer(question: str) -> dict[str, Any]:
    return graph.invoke({"messages": [("user", question)]})
```

```toml title="scrimmage.toml"
[agent]
function = "shop.graph:answer"
answer   = "messages[-1].text"

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call   = "tool:get_order"
do     = "error"
expect = [["unable", "not able", "wasn't able", "was not able", "can't", "cannot", "couldn't", "could not", "don't have access", "do not have access", "i don't have", "i don't know", "timed out", "ran into", "sorry", "unfortunately", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "issue retrieving", "issue with retrieving", "timeout issue", "i'm experiencing", "i am experiencing", "i'm currently experiencing"]]
forbid = ["shipped", "ships", "will ship", "ship soon", "ship today", "ship tomorrow", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "april", "june", "july", "august", "september", "december"]
```

A graph returns its state, not text, so `answer` says where the answer is in what the function returned.
`messages[-1].text` is the text of the last message.

The `error` fault raises LangChain's `ToolException` where the tool's own code would run, so LangChain handles it as it handles a real failure.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T195000Z-0c1d2e
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      0     0/3     0%    0-56%    1.0    0.00007  fail
range: a 95% Wilson interval, from these runs alone
fail: order-status fault 1: 0/3 passed, pass_rate is 1
  3 runs: crashed: /home/me/shop/scrimmage-runs/20261107T195000Z-0c1d2e/run-9002350ff5bc.log, /home/me/shop/scrimmage-runs/20261107T195000Z-0c1d2e/run-daa34193f3e8.log, /home/me/shop/scrimmage-runs/20261107T195000Z-0c1d2e/run-058b94b82dbf.log
model calls cost $0.0002 of a budget of $5
note: order-status fault 1: 3 crashed
```

Scrimmage ran one baseline alone first, to learn the tool names, then the rest.

The fault row fails: every run crashed.
This `ToolNode` has no error handling, so the tool's error ends the whole graph, and the agent gives no answer at all.
That is what this graph would do in production when the orders system fails.

## Fix the graph

Let the tool step turn any tool error into a message for the model:

```diff title="shop/graph.py"
-builder.add_node("tools", ToolNode(TOOLS))
+builder.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
```

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261107T195100Z-3f4a5b
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $5
```

Now the model gets a tool message that starts with "Error", and it tells the user it could not look the order up.

A mock that names a tool the graph does not have stops the test after the first baseline run.

More: [Agents on a framework](../frameworks.md).
