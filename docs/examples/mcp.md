# MCP tools

MCP, the Model Context Protocol, is a standard way for an agent to call tools that live in a separate program, an MCP server.
Agent frameworks and coding assistants use it to connect to tools run by other teams.
Scrimmage gates each MCP tool call inside your program, so a test can mock the tool or make it fail without changing the server.

## The files

A small MCP server with one tool, `search`, run over stdio:

```python title="shop/docs_server.py"
"""The help center's MCP server. In a test, its tool is mocked or failed."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("docs")


@server.tool()
def search(q: str) -> str:
    """Search the help center."""
    raise RuntimeError("the search index is not reachable from this machine")


if __name__ == "__main__":
    server.run("stdio")
```

The bot starts the server, offers its tools to the model, and runs the ones the model asks for:

```python title="shop/docs_bot.py"
"""A help bot: a loop over the openai library and the tools of one MCP server."""

import json
import sys
from typing import Any

import openai
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=sys.executable, args=["shop/docs_server.py"])


async def run_tool(session: ClientSession, name: str, arguments: str) -> str:
    try:
        result = await session.call_tool(name, json.loads(arguments))
    except MCPError as exc:  # the server did not answer
        return f"error: {exc}"
    text = " ".join(block.text for block in result.content if block.type == "text")
    return f"error: {text}" if result.is_error else text


async def answer(question: str) -> str:
    client = openai.AsyncOpenAI()
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        specs: Any = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in listed.tools
        ]
        messages: list[Any] = [{"role": "user", "content": question}]
        for _ in range(8):
            reply = await client.chat.completions.create(
                model="gpt-4o-mini", messages=messages, tools=specs
            )
            message = reply.choices[0].message
            if not message.tool_calls:
                return message.content or ""
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                content = await run_tool(session, call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return "I could not finish."
```

The label of an MCP tool is `mcp:<server>/<tool>`.
The server's name is the one it gives itself, `MCPServer("docs")`, so the label is `mcp:docs/search`.

```toml title="scrimmage.toml"
[agent]
function = "shop.docs_bot:answer"

[[test]]
name     = "refunds"
question = "How long do refunds take?"
expect   = ["five days"]

[[test.mock]]
call    = "mcp:docs/search"
returns = {result = "Refunds take five days from the day we get the item back."}

[[test.fault]]
call   = "mcp:docs/search"
do     = "timeout"
expect = [["unable", "sorry", "try again"]]
forbid = ["five days"]

[[test.fault]]
call   = "mcp:docs/search"
do     = "error"
expect = [["unable", "sorry", "try again"]]
forbid = ["five days"]
```

The server declares a result shape for a tool that returns text, with one field, `result`, so the mock is written in that shape.

## Run it

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 3 rows, 9 runs; records in /home/me/shop/scrimmage-runs/20261107T195500Z-6b7c8d
test     row       fault                    runs  fired  done  passed  rate  range    tools  cost     result
refunds  baseline  -                        3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
refunds  fault 1   mcp:docs/search timeout  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
refunds  fault 2   mcp:docs/search error    3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0003 of a budget of $5
```

The `timeout` row gets an `MCPError`, as if the server never answered.
The `error` row gets a result marked as an error, which is how a server reports a tool that failed.
The server never sees any of these calls: the mock and both faults are answered inside the bot's program.

More: [MCP tools](../mcp.md), which covers labels, what passes the gate, streamable HTTP and FastMCP.
