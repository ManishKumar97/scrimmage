"""Use the tools of an MCP server through the one registry."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from faultline_lab.tools import Registry

START_TIMEOUT = 15.0  # seconds to start the server and list its tools
LOGS_SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "faultline_lab.mcp_logs"]
)
METRICS_SERVER = StdioServerParameters(
    command=sys.executable, args=["-m", "faultline_lab.mcp_metrics"]
)


def launched(
    server: StdioServerParameters, prefix: Sequence[str], pass_env: Sequence[str]
) -> StdioServerParameters:
    """The server started under a command prefix, with the named variables passed on."""
    passed = {name: os.environ[name] for name in pass_env if name in os.environ}
    update: dict[str, object] = {"env": {**passed, **(server.env or {})}}
    if prefix:
        update.update(
            command=prefix[0], args=[*prefix[1:], server.command, *server.args]
        )
    return server.model_copy(update=update)


@asynccontextmanager
async def mcp_tools(
    server: StdioServerParameters,
    base: Registry,
    name: str = "MCP",
    call_timeout: float | None = None,
) -> AsyncIterator[Registry]:
    """Start the server, add its tools to a copy of base, and stop it on every exit.

    The server is a subprocess owned by this block. A cancel or an error inside the
    block still closes the session and ends the process. An error from inside the
    block reaches the caller as itself, not wrapped in an ExceptionGroup.
    A tool call that takes over `call_timeout` seconds fails, and the SDK tells the
    server it gave up on it.
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
                    _caller(client, tool.name, call_timeout),
                )
            yield registry
    except BaseExceptionGroup as group:
        raise _unwrap(group) from None


def _unwrap(error: BaseException) -> BaseException:
    """The one error inside nested single-error groups. Real groups stay."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _caller(client: Client, name: str, timeout: float | None = None):
    async def call(**arguments: object) -> str:
        result = await client.call_tool(name, arguments, read_timeout_seconds=timeout)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if result.is_error:
            raise RuntimeError(text or "the MCP tool failed")
        return text

    return call
