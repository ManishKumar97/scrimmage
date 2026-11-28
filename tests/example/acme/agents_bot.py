"""Acme's order bot on the OpenAI Agents SDK.

The tools are `@function_tool` tools, so a test writes no tools list. Other functions below
are the same bot set up differently for one test each.
"""

import asyncio
import os
import sys
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    RunContextWrapper,
    Runner,
    function_tool,
    handoff,
    set_tracing_disabled,
)
from agents.mcp import MCPServer
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpTool

from acme.models import Stock

set_tracing_disabled(True)  # the SDK's trace upload is a web request of its own


def stdio_args() -> list[str]:
    """How to start the documents server of the MCP tests, from the environment they set."""
    env = os.environ
    return [env["DOCS_SERVER"], "stdio", "docs", env["DOCS_LOG"], env.get("DOCS_MODE", "ok")]


@function_tool
async def get_order(order_id: str) -> str:
    """Look up an order's status and delivery date."""
    raise RuntimeError("the real orders system is not reachable from tests")


@function_tool
def check_inventory(sku: str) -> str:
    """Check how many of a product are in stock, as text."""
    return f"{sku}: 12 in stock"


@function_tool
def refund_order(order_id: str) -> str:
    """Refund an order. This moves real money."""
    raise RuntimeError(f"a real refund of {order_id} must never run in a test")


@function_tool
async def get_stock(sku: str) -> Stock:
    """Get the stock record for a product."""
    raise RuntimeError("the real stock service is not reachable from tests")


def told_down(ctx: RunContextWrapper[Any], error: Exception) -> str:
    return "the orders system is down"


@function_tool(name_override="get_order", failure_error_function=told_down)
async def get_order_told(order_id: str) -> str:
    """Look up an order; a failure is told to the model in the team's own words."""
    raise RuntimeError("the real orders system is not reachable from tests")


@function_tool(name_override="get_order", failure_error_function=None)
async def get_order_raises(order_id: str) -> str:
    """Look up an order; a failure ends the run, as the team chose."""
    raise RuntimeError("the real orders system is not reachable from tests")


@function_tool(name_override="get_order", timeout=0.5)
async def get_order_timed(order_id: str) -> str:
    """Look up an order, waiting at most half a second."""
    raise RuntimeError("the real orders system is not reachable from tests")


@function_tool(name_override="lookup_order")
async def renamed(order_id: str) -> str:
    """The model sees this as lookup_order."""
    raise RuntimeError("the real orders system is not reachable from tests")


@function_tool(name_override="get_order")
def get_order_sync(order_id: str) -> str:
    """Look up an order, in a plain function."""
    raise RuntimeError("the real orders system is not reachable from tests")


TOOLS: list[Any] = [get_order, check_inventory, refund_order, get_stock]


def run(tools: list[Any], question: str, **options: Any) -> str:
    agent = Agent(name="Acme", instructions="Answer from the tools.", tools=tools, **options)
    return str(asyncio.run(Runner.run(agent, question)).final_output)


def answer(question: str) -> str:
    return run(TOOLS, question, model="gpt-4o-mini")


def answer_told(question: str) -> str:
    return run([get_order_told, check_inventory, get_stock], question, model="gpt-4o-mini")


def answer_raises(question: str) -> str:
    return run([get_order_raises, check_inventory, get_stock], question, model="gpt-4o-mini")


def answer_timed(question: str) -> str:
    return run([get_order_timed, check_inventory, get_stock], question, model="gpt-4o-mini")


def answer_renamed(question: str) -> str:
    return run([renamed], question, model="gpt-4o-mini")


def answer_sync(question: str) -> str:
    return run([get_order_sync, check_inventory, get_stock], question, model="gpt-4o-mini")


def answer_handoff(question: str) -> str:
    """A triage agent that hands the question to a billing agent."""
    billing = Agent(name="Billing Agent", instructions="Answer billing questions.")
    triage = Agent(
        name="Triage", instructions="Route the question.", handoffs=[billing], model="gpt-4o-mini"
    )
    billing.model = "gpt-4o-mini"
    return str(asyncio.run(Runner.run(triage, question)).final_output)


def answer_handoff_with_tool(question: str) -> str:
    """The same, where the triage agent also has a tool, and the handoff is built by hand."""
    billing = Agent(name="Billing Agent", instructions="Answer.", model="gpt-4o-mini")
    triage = Agent(
        name="Triage",
        instructions="Route the question.",
        tools=[get_order],
        handoffs=[handoff(billing)],
        model="gpt-4o-mini",
    )
    return str(asyncio.run(Runner.run(triage, question)).final_output)


async def own_invoker(ctx: Any, text: str) -> str:
    raise RuntimeError("the real orders system is not reachable from tests")


def answer_custom(question: str) -> str:
    """A tool built by hand, with the team's own function in place of the SDK's invoker."""
    tool = FunctionTool(
        name="get_order",
        description="Look up an order.",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=own_invoker,
    )
    return run([tool], question, model="gpt-4o-mini")


def answer_replaced(question: str) -> str:
    """A tool whose invoker is swapped for the team's own after the tool was made."""
    get_order.on_invoke_tool = own_invoker
    return run([get_order], question, model="gpt-4o-mini")


def answer_impl_replaced(question: str) -> str:
    """A tool whose invoker is the SDK's own, with its implementation swapped after the fact."""
    tool = get_order_told
    tool.on_invoke_tool._invoke_tool_impl = own_invoker  # type: ignore[attr-defined]
    return run([tool], question, model="gpt-4o-mini")


async def ask_through_mcp(question: str) -> str:
    from agents.mcp import MCPServerStdio

    server = MCPServerStdio(
        params={"command": sys.executable, "args": stdio_args()}, client_session_timeout_seconds=30
    )
    async with server:
        agent = Agent(
            name="Acme", instructions="Answer.", mcp_servers=[server], model="gpt-4o-mini"
        )
        return str((await Runner.run(agent, question)).final_output)


def answer_mcp(question: str) -> str:
    """The same bot with its tools from an MCP server, which the SDK turns into function tools."""
    return asyncio.run(ask_through_mcp(question))


class InProcessServer(MCPServer):
    """An MCP server of the team's own: its call_tool never goes through an MCP session."""

    @property
    def name(self) -> str:
        return "inproc"

    async def connect(self) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[McpTool]:
        del run_context, agent
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        return [McpTool(name="search", description="Search the documents.", input_schema=schema)]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        touch("server")
        return CallToolResult(content=[TextContent(type="text", text="REAL SERVER RAN")])

    async def list_prompts(self) -> Any:
        return None

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return None


def touch(who: str) -> None:
    """Leave a file saying the team's code ran, where the test says."""
    with open(os.environ["ACME_TOUCH"], "a") as out:
        out.write(f"{who}\n")


def answer_inprocess_mcp(question: str) -> str:
    async def ask() -> str:
        agent = Agent(
            name="Acme",
            instructions="Answer.",
            mcp_servers=[InProcessServer()],
            model="gpt-4o-mini",
        )
        return str((await Runner.run(agent, question)).final_output)

    return asyncio.run(ask())


def on_billing(context: Any) -> None:
    touch("callback")


def answer_handoff_callback(question: str) -> str:
    """A handoff with the team's callback, as the airline demo's agents have."""
    billing = Agent(name="Billing Agent", instructions="Answer.", model="gpt-4o-mini")
    triage = Agent(
        name="Triage",
        instructions="Route the question.",
        handoffs=[handoff(agent=billing, on_handoff=on_billing)],
        model="gpt-4o-mini",
    )
    return str(asyncio.run(Runner.run(triage, question)).final_output)


@function_tool(name_override="transfer_to_billing_agent")
async def collider(note: str) -> str:
    """A tool named like the handoff below."""
    touch("tool named like a handoff")
    return "x"


def answer_handoff_collision(question: str) -> str:
    """A handoff named like a tool no gate has seen made."""
    billing = Agent(name="Billing Agent", instructions="Answer.", model="gpt-4o-mini")
    unseen = object.__new__(FunctionTool)  # made with no __post_init__, so never tracked
    unseen.__dict__.update(collider.__dict__)
    triage = Agent(
        name="Triage",
        instructions="Route the question.",
        tools=[unseen],
        handoffs=[handoff(billing)],
        model="gpt-4o-mini",
    )
    return str(asyncio.run(Runner.run(triage, question)).final_output)


def answer_own_call(question: str) -> str:
    """A tool whose invoker class replaces __call__ and never reaches the implementation."""
    from agents.tool import _FailureHandlingFunctionToolInvoker as Invoker  # type: ignore

    class Mine(Invoker):  # type: ignore[misc]
        async def __call__(self, ctx: Any, input: str) -> Any:
            touch("own call")
            return "bypassed"

    tool = FunctionTool(
        name="get_order",
        description="Look up an order.",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=own_invoker,
    )
    # The SDK rebinds an invoker given to the constructor; one set afterwards is kept.
    tool.on_invoke_tool = Mine(own_invoker, lambda *a: None, function_tool=tool)
    return run([tool], question, model="gpt-4o-mini")
