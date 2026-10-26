"""Use the tools of an MCP server through the one registry."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from faultline.tools import Registry

START_TIMEOUT = 15.0  # seconds to start the server and list its tools
LOGS_SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "faultline.mcp_logs"]
)
METRICS_SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "faultline.mcp_metrics"]
)


@asynccontextmanager
async def mcp_tools(
    server: StdioServerParameters, base: Registry, name: str = "MCP"
) -> AsyncIterator[Registry]:
    """Start the server, add its tools to a copy of base, and stop it on every exit.

    The server is a subprocess owned by this block. A cancel or an error inside the
    block still closes the session and ends the process. An error from inside the
    block reaches the caller as itself, not wrapped in an ExceptionGroup.
    """
    registry = base.copy()
    # The SDK passes a child only a few variables. The kube client needs its config path.
    kube = {k: v for k, v in os.environ.items() if k.startswith("KUBE")}
    server = server.model_copy(update={"env": {**kube, **(server.env or {})}})
    try:
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(START_TIMEOUT):
                    client = await stack.enter_async_context(Client(server))
                    listed = await client.list_tools()
            except TimeoutError:
                raise RuntimeError(
                    f"{name} server did not start within {START_TIMEOUT:g}s"
                ) from None
            for tool in listed.tools:
                registry.remote(
                    tool.name,
                    tool.description or "",
                    tool.input_schema,
                    _caller(client, tool.name),
                )
            yield registry
    except BaseExceptionGroup as group:
        raise _unwrap(group) from None


def _unwrap(error: BaseException) -> BaseException:
    """The one error inside nested single-error groups. Real groups stay."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _caller(client: Client, name: str):
    async def call(**arguments: object) -> str:
        result = await client.call_tool(name, arguments)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if result.is_error:
            raise RuntimeError(text or "the MCP tool failed")
        return text

    return call
