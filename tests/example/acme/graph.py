"""Acme's order bot in LangGraph: a model node and a tool node, in a loop.

The tools are LangChain tools, so a test writes no tools list. Other functions below are
the same bot with a tool set up differently for one test each.
"""

import asyncio
import subprocess
import sys
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, Tool, ToolException, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import (
    InjectedState,
    InjectedStore,
    ToolNode,
    ToolRuntime,
    tools_condition,
)
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, ConfigDict

from acme.models import Stock


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    secret: NotRequired[str]  # never sent to the model


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


def build(tools: list[BaseTool], store: Any = None) -> Any:
    model = ChatOpenAI(model="gpt-4o-mini", max_retries=0).bind_tools(tools)

    def think(state: State) -> dict[str, list[Any]]:
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(State)
    graph.add_node("think", think)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "think")
    return graph.compile(store=store)


def answer(question: str) -> dict[str, Any]:
    return build(TOOLS).invoke({"messages": [("user", question)]})


async def answer_async(question: str) -> dict[str, Any]:
    return await build(TOOLS).ainvoke({"messages": [("user", question)]})


def answer_after_a_child(question: str) -> dict[str, Any]:
    """A Python child ends first and has none of the agent's tools."""
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    return answer(question)


def answer_with_runtime(question: str) -> dict[str, Any]:
    """get_order reads the conversation through LangGraph's runtime argument."""

    @tool
    def get_order(order_id: str, runtime: ToolRuntime) -> str:
        """Look up an order's status and delivery date."""
        return str(len(runtime.state["messages"]))

    return build([get_order, get_stock]).invoke({"messages": [("user", question)]})


def answer_with_injected(question: str) -> dict[str, Any]:
    """get_order takes a state field and the store, which LangGraph passes in."""

    @tool
    def get_order(
        order_id: str,
        note: Annotated[str, InjectedState("secret")],
        store: Annotated[Any, InjectedStore()],
    ) -> str:
        """Look up an order's status and delivery date."""
        return note

    bot = build([get_order, get_stock], InMemoryStore())
    return bot.invoke({"messages": [("user", question)], "secret": "the hidden note"})


class InjectedArgs(BaseModel):
    """What the model supplies, beside what LangGraph fills in."""

    order_id: str
    note: Annotated[str, InjectedState("secret")]
    ctx: Annotated[Any, InjectedStore()]


class RuntimeArgs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    order_id: str
    runtime: ToolRuntime


class SchemaClass(BaseTool):
    name: str = "get_order"
    description: str = "Look up an order's status and delivery date."
    args_schema: type[BaseModel] = InjectedArgs  # type: ignore[assignment]

    def _run(self, order_id: str, note: str, ctx: Any) -> str:
        return note


def with_secret(tool_object: BaseTool, question: str) -> dict[str, Any]:
    bot = build([tool_object, get_stock], InMemoryStore())
    return bot.invoke({"messages": [("user", question)], "secret": "the hidden note"})


class OnlyInjected(BaseModel):
    note: Annotated[str, InjectedState("secret")]


def single_input_tool(question: str) -> dict[str, Any]:
    """LangChain passes a single-input Tool's one value by position."""
    return with_secret(
        Tool(name="get_order", func=str, description="Look up.", args_schema=OnlyInjected),
        question,
    )


def schema_on_the_decorator(question: str) -> dict[str, Any]:
    @tool(args_schema=InjectedArgs)
    def get_order(order_id: str, note: str, ctx: Any) -> str:
        """Look up an order's status and delivery date."""
        return note

    return with_secret(get_order, question)


def schema_on_the_class(question: str) -> dict[str, Any]:
    return with_secret(SchemaClass(), question)


def runtime_in_the_schema(question: str) -> dict[str, Any]:
    @tool(args_schema=RuntimeArgs)
    def get_order(order_id: str, runtime: Any) -> str:
        """Look up an order's status and delivery date."""
        return str(len(runtime.state["messages"]))

    return with_secret(get_order, question)


class OrderArgs(BaseModel):
    order_id: str


def runtime_beside_the_schema(question: str) -> dict[str, Any]:
    """The signature takes the runtime, and the schema does not mention it."""

    @tool(args_schema=OrderArgs)
    def get_order(order_id: str, runtime: ToolRuntime) -> str:
        """Look up an order's status and delivery date."""
        return str(len(runtime.state["messages"]))

    return with_secret(get_order, question)


def dict_schema(question: str) -> dict[str, Any]:
    @tool(
        args_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }
    )
    def get_order(order_id: str) -> str:
        """Look up an order's status and delivery date."""
        return order_id

    return with_secret(get_order, question)


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


class RunsItself:
    """A mixin holding the tool's work: it is defined on no BaseTool subclass."""

    def _run(self, order_id: str = "1") -> str:
        return "REAL WRITE RAN"


class MixinTool(RunsItself, BaseTool):
    name: str = "mixin_write"
    description: str = "A write tool whose work is on a mixin."


def mixin_tool(question: str) -> str:
    return str(MixinTool().invoke({"order_id": "1"}))


class OverridesInvoke(BaseTool):
    name: str = "overrides_invoke"
    description: str = "A tool that answers without passing through run."

    def _run(self) -> str:
        return "real"

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> str:
        return "REAL WRITE RAN"


def answer_overridden(question: str) -> dict[str, Any]:
    return build([OverridesInvoke(), get_stock]).invoke({"messages": [("user", question)]})


@tool(response_format="content_and_artifact")
def get_report(order_id: str) -> tuple[str, dict[str, str]]:
    """Get an order's report and its raw data."""
    raise RuntimeError("the real reports system is not reachable from tests")


def report_tool(question: str) -> str:
    return str(get_report.invoke({"order_id": "1"}))


@tool
def takes_config(sku: str, cfg: RunnableConfig) -> str:
    """A tool that reads LangChain's config, whatever it calls it."""
    return str(cfg.get("configurable", {}).get("secret", "none"))


class NamedConfig(BaseTool):
    name: str = "named_config"
    description: str = "A tool with a real input called config."

    def _run(self, config: str = "") -> str:  # type: ignore[override]
        return config


class UntypedManager(BaseTool):
    name: str = "untyped_manager"
    description: str = "A tool that takes LangChain's run manager with no annotation."

    def _run(self, sku: str, run_manager=None) -> str:  # type: ignore[no-untyped-def, override]
        return sku


def plumbing(question: str) -> str:
    takes_config.invoke({"sku": "SKU-42"}, {"configurable": {"secret": "hidden-value"}})
    UntypedManager().invoke({"sku": "SKU-42"})
    return str(NamedConfig()._run(config="kept"))


def answer_handled_text(question: str) -> dict[str, Any]:
    """The team gave LangChain a fixed message for tool failures."""
    get_order.handle_tool_error = "the orders system is down"
    return answer(question)


def answer_handled_callable(question: str) -> dict[str, Any]:
    """The team gave LangChain a function for tool failures."""
    get_order.handle_tool_error = lambda error: f"handled: {error}"
    return answer(question)


async def answer_handled_async(question: str) -> dict[str, Any]:
    get_order.handle_tool_error = True
    return await answer_async(question)


@tool("lookup_order")
def find_order(order_id: str) -> str:
    """Look up an order, under another name for the model."""
    raise RuntimeError("the real orders system is not reachable from tests")


def renamed(question: str) -> str:
    return str(find_order.invoke({"order_id": "1"}))


def plain_tool(question: str) -> str:
    def look_up(order_id: str) -> str:
        raise RuntimeError("the real orders system is not reachable from tests")

    return str(Tool(name="get_order", func=look_up, description="Look up an order.").invoke("1"))


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
