"""A tiny MCP server for tests. Tools: pid, echo, fail, hang."""

import asyncio
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("test")


@server.tool()
def pid() -> str:
    """The server process id."""
    return str(os.getpid())


@server.tool()
def echo(text: str) -> str:
    """Echo the text."""
    return f"echo {text}"


@server.tool()
def fail(reason: str) -> str:
    """Always fails."""
    raise ToolError(reason)


@server.tool()
async def hang() -> str:
    """Never answers."""
    await asyncio.sleep(3600)
    return "late"


if __name__ == "__main__":
    server.run("stdio")
