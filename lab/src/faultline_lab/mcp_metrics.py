"""The metrics tools as an MCP server. Run it with: python -m faultline_lab.mcp_metrics"""

import functools
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from faultline_lab.metrics_tools import get_error_rate_trend, get_service_metrics

server = MCPServer("faultline-metrics")


def readable_errors[**P](
    function: Callable[P, Awaitable[str]],
) -> Callable[P, Awaitable[str]]:
    """Let a RuntimeError reach the model. The SDK hides the text of other exceptions."""

    @functools.wraps(function)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return await function(*args, **kwargs)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


server.tool()(readable_errors(get_service_metrics))
server.tool()(readable_errors(get_error_rate_trend))

if __name__ == "__main__":
    server.run("stdio")
