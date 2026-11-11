"""A real MCP server, served by the official library, for the MCP gate's tests.

python docs_server.py <stdio|http|sse> <name> <log> <mode> [port]

Every tool call it really receives goes on a line of the log, so a test can prove that a
call never arrived. The mode is how its tools really behave: ok, fail, wrong, empty, slow.
"""

import sys
import time
from pathlib import Path
from typing import Literal, TypedDict

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import (
    CallToolResult,
    CreateMessageRequestParams,
    ImageContent,
    SamplingMessage,
    TextContent,
    ToolAnnotations,
)


class Hits(TypedDict):
    hits: list[str]
    total: int


class Ticket(TypedDict):
    status: Literal["open", "closed"]
    note: str


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
        return {"hits": [f"réal {q}" if mode == "accent" else f"real {q}"], "total": 1}

    @server.tool(structured_output=False)
    def note(q: str) -> list[TextContent | ImageContent]:
        received("note", q)
        if mode == "two":
            return [TextContent(type="text", text="one"), TextContent(type="text", text="two")]
        if mode == "image":
            return [ImageContent(type="image", data="iVBORw0KGgo=", mime_type="image/png")]
        return [] if mode == "empty" else [TextContent(type="text", text=f"real note {q}")]

    @server.tool(structured_output=False)
    async def chatty(q: str, ctx: Context) -> str:
        """Sends the client what a callback handles, on this call's own reply stream."""
        received("chatty", q)
        await ctx.info("working")
        await ctx.report_progress(1, 2)
        if q == "sample":
            message = SamplingMessage(role="user", content=TextContent(type="text", text=q))
            params = CreateMessageRequestParams(messages=[message], max_tokens=5)
            await ctx.session.create_message(params.messages, max_tokens=params.max_tokens)
        return "done"

    @server.resource("docs://handbook")
    def handbook() -> str:
        received("resources/read", "handbook")
        return "the handbook"

    @server.prompt()
    def summarise(topic: str) -> str:
        received("prompts/get", topic)
        return f"summarise {topic}"

    @server.tool()
    def title(q: str) -> str:
        received("title", q)
        return f"real title {q}"

    @server.tool()
    def ticket(q: str) -> Ticket:
        received("ticket", q)
        return {"status": "open", "note": f"real ticket {q}"}

    @server.tool(structured_output=False)
    def delete_doc(doc: str) -> str:
        received("delete_doc", doc)
        return f"deleted {doc}"

    # Names that collide with a label of the form server/tool.
    awkward = {"wiki": "docs/search", "a": "b/c", "a/b": "c"}.get(name)
    if awkward is not None:

        def slashed(q: str) -> str:
            received(awkward, q)
            return "real"

        server.add_tool(slashed, name=awkward, structured_output=False)

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
