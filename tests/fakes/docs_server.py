"""A real MCP server, served by the official library, for the MCP gate's tests.

python docs_server.py <stdio|http|sse> <name> <log> <mode> [port]

Every tool call it really receives goes on a line of the log, so a test can prove that a
call never arrived. The mode is how its tools really behave: ok, fail, wrong, empty, slow.
"""

import sys
import time
from pathlib import Path
from typing import TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations


class Hits(TypedDict):
    hits: list[str]
    total: int


def serve(transport: str, name: str, log: str, mode: str, port: int = 0) -> None:
    server = MCPServer(name)

    def received(tool: str, value: str) -> None:
        with Path(log).open("a") as file:
            file.write(f"{tool} {value}\n")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def search(q: str) -> Hits:
        received("search", q)
        if mode == "fail":
            raise ValueError("the index is down")
        if mode == "slow":
            time.sleep(30)
        if mode == "empty":
            return CallToolResult(content=[])  # type: ignore[return-value]
        if mode == "wrong":
            bad = CallToolResult(
                content=[TextContent(type="text", text="bad")],
                structured_content={"hits": "not a list"},
            )
            return bad  # type: ignore[return-value]
        return {"hits": [f"real {q}"], "total": 1}

    @server.tool(structured_output=False)
    def note(q: str) -> list[TextContent]:
        received("note", q)
        return [] if mode == "empty" else [TextContent(type="text", text=f"real note {q}")]

    @server.tool()
    def title(q: str) -> str:
        received("title", q)
        return f"real title {q}"

    @server.tool(structured_output=False)
    def delete_doc(doc: str) -> str:
        received("delete_doc", doc)
        return f"deleted {doc}"

    # A schema with no model behind it is published but not enforced, so this server can
    # really send a reply that breaks its own schema.
    for tool in server._tool_manager.list_tools():
        if tool.name == "search" and mode in ("wrong", "empty"):
            tool.fn_metadata.output_model = None

    if transport == "stdio":
        server.run("stdio")
    elif transport == "sse":
        server.run("sse", host="127.0.0.1", port=port)
    else:
        server.run("streamable-http", host="127.0.0.1", port=port)


if __name__ == "__main__":
    a = sys.argv[1:]
    serve(a[0], a[1], a[2], a[3], int(a[4]) if len(a) > 4 else 0)
