"""Acme's order bot in LangGraph: a model node and a tool node, in a loop.

The tools are LangChain tools, so a test writes no tools list. Other functions below are
the same bot with a tool set up differently for one test each.
"""

import asyncio
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from acme.models import Stock


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@tool
def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    raise RuntimeError("the real orders system is not reachable from tests")


@tool
def check_inventory(sku: str) -> str:
    """Check how many of a product are in stock, as text."""
    return f"{sku}: 12 in stock"


@tool
def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    raise RuntimeError(f"a real refund of {order_id} must never run in a test")


@tool
def get_stock(sku: str) -> Stock:
    """Get the stock record for a product."""
    raise RuntimeError("the real stock service is not reachable from tests")


TOOLS: list[BaseTool] = [get_order, check_inventory, refund_order, get_stock]


def build(tools: list[BaseTool]) -> Any:
    model = ChatOpenAI(model="gpt-4o-mini", max_retries=0).bind_tools(tools)

    def think(state: State) -> dict[str, list[Any]]:
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(State)
    graph.add_node("think", think)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "think")
    return graph.compile()


def answer(question: str) -> dict[str, Any]:
    return build(TOOLS).invoke({"messages": [("user", question)]})


async def answer_async(question: str) -> dict[str, Any]:
    return await build(TOOLS).ainvoke({"messages": [("user", question)]})


def answer_handled(question: str) -> dict[str, Any]:
    """The team asked LangChain to hand tool failures to the model."""
    get_order.handle_tool_error = True
    return answer(question)


class CustomTool(BaseTool):
    """A tool written as a class, not with the decorator."""

    name: str = "get_order"
    description: str = "Look up an order's status and delivery date."

    def _run(self, order_id: str) -> str:
        raise ToolException("orders system unavailable")


def answer_custom_class(question: str) -> dict[str, Any]:
    return build([CustomTool(), get_stock]).invoke({"messages": [("user", question)]})


@tool
async def get_order_async(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    await asyncio.sleep(0)
    raise RuntimeError("the real orders system is not reachable from tests")


def real_failure(question: str) -> dict[str, Any]:
    """get_order really raises ToolException; the same bot as the injected row."""

    @tool
    def get_order(order_id: str) -> str:
        """Look up an order's status and delivery date."""
        raise ToolException("get_order failed")

    return build([get_order, get_stock]).invoke({"messages": [("user", question)]})


def real_failure_handled(question: str) -> dict[str, Any]:
    @tool
    def get_order(order_id: str) -> str:
        """Look up an order's status and delivery date."""
        raise ToolException("get_order failed")

    get_order.handle_tool_error = True
    return build([get_order, get_stock]).invoke({"messages": [("user", question)]})


def answer_unseen(question: str) -> str:
    """Offers the model one tool no gate reaches, beside the ones the adapter gates."""
    unseen = {
        "type": "function",
        "function": {"name": "delete_all", "parameters": {"type": "object"}},
    }
    model = ChatOpenAI(model="gpt-4o-mini", max_retries=0).bind_tools([*TOOLS, unseen])
    return str(model.invoke(question).content)


def nested(question: str) -> str:
    """A tool that calls another tool: both are calls the gate must see."""

    @tool
    def outer(sku: str) -> str:
        """Call another tool."""
        return check_inventory.invoke({"sku": sku})

    return str(outer.invoke({"sku": "SKU-42"}))


def sync_tool_through_arun(question: str) -> str:
    """_arun of a tool with no coroutine runs _run: one call, one record line."""

    async def main() -> str:
        return str(await check_inventory.arun({"sku": "SKU-42"}))

    return asyncio.run(main())


def async_tool(question: str) -> str:
    return str(asyncio.run(get_order_async.ainvoke({"order_id": "1234"})))
