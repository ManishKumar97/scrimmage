"""The logs tool as an MCP server. Run it with: python -m faultline_lab.mcp_logs"""

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from faultline_lab.kube_tools import get_logs

server = MCPServer("faultline-logs")


@server.tool()
@functools.wraps(get_logs)
async def get_logs_tool(service: str) -> str:
    # The SDK hides the text of an unexpected exception from the client. These errors
    # (no such deployment, timeout) are written for the model, so they pass as ToolError.
    try:
        return await get_logs(service)
    except RuntimeError as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    server.run("stdio")
