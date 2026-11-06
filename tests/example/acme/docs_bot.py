"""Acme's bot that searches documents through an MCP server, over stdio or over the web.

DOCS_TRANSPORT says which. DOCS_SERVER, DOCS_LOG and DOCS_MODE say how to start a stdio
server; DOCS_URL is a running server's address. DOCS_TOOL is the tool to call.
"""

import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
import httpx2
from mcp import Client, ClientSession, MCPError, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CreateMessageResult, EmptyResult, PromptReference, Request, TextContent

LIMIT = float(os.environ.get("DOCS_TIMEOUT", "20"))
TOOL = os.environ.get("DOCS_TOOL", "search")


def stdio_args(name: str = "docs") -> list[str]:
    env = os.environ
    return [env["DOCS_SERVER"], "stdio", name, env["DOCS_LOG"], env.get("DOCS_MODE", "ok")]


@asynccontextmanager
async def over_stdio(
    name: str = "docs", env: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    server = StdioServerParameters(command=sys.executable, args=stdio_args(name), env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


@asynccontextmanager
async def over_http() -> AsyncIterator[ClientSession]:
    async with (
        streamable_http_client(os.environ["DOCS_URL"]) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def connected() -> Any:
    return over_http() if os.environ.get("DOCS_TRANSPORT") == "http" else over_stdio()


def text_of(result: Any) -> str:
    return " ".join(block.text for block in result.content)


def described(result: Any) -> str:
    """Everything the bot can see in a reply."""
    shape = result.structured_content
    blocks = len(result.content)
    return f"error={result.is_error} blocks={blocks} text={text_of(result)!r} structured={shape}"


async def search(session: ClientSession) -> str:
    """The bot's own error handling, which a fault must reach."""
    try:
        result = await session.call_tool(TOOL, {"q": "refunds"}, read_timeout_seconds=LIMIT)
    except MCPError as exc:
        return f"MCPError code={exc.code} message={exc.message}"
    except RuntimeError as exc:
        return f"RuntimeError {exc}"
    return described(result)


def answer(question: str) -> str:
    async def go() -> str:
        async with connected() as session:
            await session.list_tools()
            return await search(session)

    return asyncio.run(go())


def answer_twice(question: str) -> str:
    async def go() -> str:
        async with connected() as session:
            await session.list_tools()
            return f"{await search(session)} then {await search(session)}"

    return asyncio.run(go())


def find_docs(question: str) -> str:
    """A Python tool that makes its own MCP call."""
    return answer(question)


def answer_through_a_tool(question: str) -> str:
    return find_docs(question)


def answer_not_listed(question: str) -> str:
    """The bot calls a tool without listing it first."""

    async def go() -> str:
        async with connected() as session:
            return await search(session)

    return asyncio.run(go())


def answer_with_the_runs_environment(question: str) -> str:
    """The server inherits everything, Derail's own variables too."""

    async def go() -> str:
        async with over_stdio(env=dict(os.environ)) as session:
            await session.list_tools()
            return await search(session)

    return asyncio.run(go())


def answer_swallows_refusal(question: str) -> str:
    """A framework may turn a refusal into a tool result and try another tool."""

    async def go() -> str:
        async with over_stdio() as session:
            try:
                await session.call_tool("delete_doc", {"doc": "handbook"})
            except Exception as exc:
                print(f"swallowed {type(exc).__name__}", file=sys.stderr)
            return await search(session)

    return asyncio.run(go())


def answer_keeps_a_secret(question: str) -> str:
    async def go() -> str:
        async with over_stdio() as session:
            await session.list_tools()
            key = "sk-" + "a" * 40
            return described(await session.call_tool("search", {"q": f"token {key}"}))

    return asyncio.run(go())


def answer_two_servers(question: str) -> str:
    """Two servers, each with a search tool."""

    async def go() -> str:
        async with over_stdio("docs") as docs, over_stdio("wiki") as wiki:
            await docs.list_tools()
            await wiki.list_tools()
            return await search(docs)

    return asyncio.run(go())


def answer_with_reads(question: str) -> str:
    """Requests that are not tool calls, though a server can act on them."""

    async def go() -> str:
        async with over_stdio() as session:
            await session.read_resource("docs://handbook")
            await session.get_prompt("summarise", {"topic": "refunds"})
            reference = PromptReference(type="ref/prompt", name="summarise")
            for attempt in (
                session.complete(ref=reference, argument={"name": "topic", "value": "r"}),
                session.subscribe_resource("docs://handbook"),
            ):
                try:
                    await attempt
                except MCPError as exc:
                    print(f"the server said no: {exc.message}", file=sys.stderr)
            return "read"

    return asyncio.run(go())


def answer_web_after_mcp(question: str) -> str:
    """A write of the agent's own to the MCP server's host is not an MCP message."""

    async def go() -> str:
        async with over_http() as session:
            await session.list_tools()
            first = await search(session)
            base = os.environ["DOCS_URL"].rsplit("/", 1)[0]
            async with httpx.AsyncClient() as client:
                await client.post(f"{base}/orders", json={"order": 1})
            return first

    return asyncio.run(go())


def answer_web_after_close(question: str) -> str:
    """The transport's close runs in the agent's own task, and must leave nothing behind."""

    async def go() -> str:
        async with over_http() as session:
            await session.list_tools()
        base = os.environ["DOCS_URL"].rsplit("/", 1)[0]
        async with httpx.AsyncClient() as client:
            await client.post(f"{base}/orders", json={"order": 1})
        return "posted"

    return asyncio.run(go())


def answer_unknown_request(question: str) -> str:
    """A request of a newer protocol, or of an extension."""

    class RunTask(Request[None, Literal["tasks/run"]]):
        method: Literal["tasks/run"] = "tasks/run"
        params: None = None

    async def go() -> str:
        async with connected() as session:
            reply = await session.send_request(RunTask(), EmptyResult)
            return repr(reply)

    return asyncio.run(go())


def answer_web_in_a_hook(question: str) -> str:
    """Code that runs inside the transport's own send still meets the web gate."""

    async def hook(response: Any) -> None:
        async with httpx2.AsyncClient() as other:
            await other.post(os.environ["DOCS_TARGET"])

    async def go() -> str:
        client = httpx2.AsyncClient(event_hooks={"response": [hook]})
        async with (
            client,
            streamable_http_client(os.environ["DOCS_URL"], http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return "connected"

    return asyncio.run(go())


class TokenAuth(httpx2.Auth):
    """Fetches a token from a path of the MCP server's own host, inside the send."""

    async def async_auth_flow(self, request: Any) -> AsyncGenerator[Any, Any]:
        yield httpx2.Request("POST", os.environ["DOCS_TARGET"])
        yield request


def answer_with_a_token(question: str) -> str:
    async def go() -> str:
        client = httpx2.AsyncClient(auth=TokenAuth())
        async with (
            client,
            streamable_http_client(os.environ["DOCS_URL"], http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await session.list_tools()
            return await search(session)

    return asyncio.run(go())


async def write_from_a_callback() -> None:
    async with httpx2.AsyncClient() as client:
        await client.post(os.environ["DOCS_TARGET"])


async def on_log(params: Any) -> None:
    await write_from_a_callback()


async def on_progress(progress: float, total: float | None, message: str | None) -> None:
    del progress, total, message
    await write_from_a_callback()


async def on_sampling(context: Any, params: Any) -> Any:
    await write_from_a_callback()
    return CreateMessageResult(
        role="assistant", content=TextContent(type="text", text="x"), model="m"
    )


async def on_sampling_ask_the_model(context: Any, params: Any) -> Any:
    """A sampling callback is where a bot really calls its model."""
    from openai import AsyncOpenAI

    reply = await AsyncOpenAI().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}], max_tokens=5
    )
    text = reply.choices[0].message.content or ""
    return CreateMessageResult(
        role="assistant", content=TextContent(type="text", text=text), model="gpt-4o-mini"
    )


def answer_from_a_callback(question: str) -> str:
    """The server answers on the POST's own stream, and the bot's callback writes."""
    kind = os.environ["DOCS_CALLBACK"]

    async def go() -> str:
        async with streamable_http_client(os.environ["DOCS_URL"]) as (read, write):
            options: dict[str, Any] = {}
            if kind == "logging":
                options["logging_callback"] = on_log
            if kind == "sampling":
                options["sampling_callback"] = on_sampling
            if kind == "model":
                options["sampling_callback"] = on_sampling_ask_the_model
            async with ClientSession(read, write, **options) as session:
                await session.initialize()
                sample = kind in ("sampling", "model")
                result = await session.call_tool(
                    "chatty",
                    {"q": "sample" if sample else "plain"},
                    progress_callback=on_progress if kind == "progress" else None,
                )
                return described(result)

    return asyncio.run(go())


def answer_newer_protocol(question: str) -> str:
    """The newer protocol: no initialize, and the server's name comes from server/discover."""

    async def go() -> str:
        async with Client(os.environ["DOCS_URL"]) as client:
            await client.list_tools()
            result = await client.call_tool("search", {"q": "refunds"})
            return f"{client.session.protocol_version} {described(result)}"

    return asyncio.run(go())


def answer_sse(question: str) -> str:
    async def go() -> str:
        async with (
            sse_client(os.environ["DOCS_URL"]) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await search(session)

    return asyncio.run(go())


def answer_fastmcp(question: str) -> str:
    from fastmcp import Client as FastClient
    from fastmcp.client.transports import StdioTransport
    from fastmcp.exceptions import ToolError

    async def go() -> str:
        if os.environ.get("DOCS_TRANSPORT") == "http":
            client = FastClient(os.environ["DOCS_URL"])
        else:
            client = FastClient(StdioTransport(sys.executable, stdio_args()))
        async with client:
            await client.list_tools()
            try:
                result = await client.call_tool(TOOL, {"q": "refunds"}, timeout=LIMIT)
            except ToolError as exc:
                return f"ToolError {exc}"
            except MCPError as exc:
                return f"MCPError code={exc.code} message={exc.message}"
            except RuntimeError as exc:
                return f"RuntimeError {exc}"
            return f"text={[b.text for b in result.content]} data={result.data}"  # type: ignore[union-attr]

    return asyncio.run(go())


async def model_loop(question: str, session: ClientSession) -> str:
    """The loop: offer the server's tools to the model, run what it asks for, feed it back."""
    from openai import OpenAI

    client = OpenAI()
    listed = await session.list_tools()
    tools: list[Any] = [
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
    for _ in range(4):
        reply = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, tools=tools, max_tokens=300
        )
        message = reply.choices[0].message
        if not message.tool_calls:
            return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for tool_call in message.tool_calls:
            function: Any = getattr(tool_call, "function")  # noqa: B009
            try:
                result = await session.call_tool(function.name, json.loads(function.arguments))
                content = text_of(result)
                content = f"tool failed: {content}" if result.is_error else content
            except MCPError as exc:
                content = f"tool failed: {exc.message}"
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})
    return "gave up"


def answer_model(question: str) -> str:
    async def go() -> str:
        async with connected() as session:
            return await model_loop(question, session)

    return asyncio.run(go())
