"""Use the tools of an MCP server through the one registry."""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from faultline.tools import Registry

LOGS_SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "faultline.mcp_logs"]
)


@asynccontextmanager
async def mcp_tools(
    server: StdioServerParameters, base: Registry
) -> AsyncIterator[Registry]:
    """Start the server, add its tools to a copy of base, and stop it on every exit.

    The server is a subprocess owned by this block. A cancel or an error inside the
    block still closes the session and ends the process.
    """
    registry = base.copy()
    async with Client(server) as client:
        for tool in (await client.list_tools()).tools:
            registry.remote(
                tool.name,
                tool.description or "",
                tool.input_schema,
                _caller(client, tool.name),
            )
        yield registry


def _caller(client: Client, name: str):
    async def call(**arguments: object) -> str:
        result = await client.call_tool(name, arguments)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if result.is_error:
            raise RuntimeError(text or "the MCP tool failed")
        return text

    return call
