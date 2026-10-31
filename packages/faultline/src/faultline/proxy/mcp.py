"""`faultline mcp-wrap -- CMD...`: run a local MCP server with its traffic through Faultline.

A local MCP server talks JSON-RPC over stdin and stdout, one message per line.
The agent's MCP config starts this wrapper in front of the server's command:

    faultline mcp-wrap -- python -m my_mcp_server

The wrapper runs the server as its child and relays every line both ways unchanged.
It watches each `tools/call` request and the response with the same JSON-RPC id, and
records the pair as a tool call. The server's stderr passes straight through.
Under `faultline run`, each tool call becomes an `execute_tool` span in the run's trace,
and a tool named in a `tool:NAME:KIND` fault is answered by the fault, not the server.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from faultline.faults import (
    FaultShapeError,
    ToolFault,
    ToolInjector,
    ToolRequest,
    ToolSession,
    parse_all,
)
from faultline.model import ToolCall
from faultline.proxy.context import find as find_run
from faultline.proxy.record import Recorder
from faultline.relay import RELAYED, Relay, tied_to_parent

# The error type on the span of a faked result that does not fit the tool's schema.
SHAPE_INVALID = "fault_shape_invalid"
# The longest line, in bytes. A tool result can be large, such as many log lines.
LINE_LIMIT = 128 * 1024 * 1024


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "mcp-wrap",
        help="run a local MCP server with its tool calls through faultline",
        description="Run CMD, a stdio MCP server, and relay its traffic.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- CMD...")
    parser.set_defaults(handler=wrap)


def wrap(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("faultline mcp-wrap: give the server's command after --", file=sys.stderr)
        return 2
    # Under `faultline run`, the wrapper writes its own span file and applies the
    # run's tool faults. Else it only relays.
    try:
        run = find_run()
        faults = parse_all(run.faults if run else ())
    except (OSError, ValueError, TypeError, KeyError) as exc:  # FaultError too
        print(f"faultline mcp-wrap: cannot read the run: {exc}", file=sys.stderr)
        return 2
    recorder = Recorder.for_run(run, f"mcp-{os.getpid()}.jsonl") if run else None
    tool_faults = [fault for fault in faults if isinstance(fault, ToolFault)]
    calls = ToolCalls(recorder.tool_call if recorder else None, tool_faults)
    return asyncio.run(relay(command, calls))


@dataclass
class Pending:
    name: str
    arguments: str
    started: int  # unix nanoseconds
    clock: float  # monotonic seconds, for the duration


class ToolCalls:
    """Pairs each tools/call request with its response, by JSON-RPC id."""

    def __init__(
        self,
        on_call: Callable[[ToolCall], None] | None = None,
        faults: Sequence[ToolFault] = (),
    ) -> None:
        self.pending: dict[str, Pending] = {}
        self.calls: list[ToolCall] = []
        self.on_call = on_call
        # Each fault's injector, by the tool it targets. A tool may have several,
        # such as one fault on its first call and another on its second.
        self.injectors: dict[str, list[ToolInjector]] = {}
        for fault in faults:
            self.injectors.setdefault(fault.tool, []).append(fault.injector())
        self.counts: dict[str, int] = {}  # calls of each tool so far
        # What a faked result must hold to pass for the server's own, learned from it.
        self.listing: set[str] = set()  # ids of tools/list requests in flight
        self.schemas: dict[str, dict] = {}  # each tool's declared outputSchema
        self.tags_results = False  # results carry resultType (MCP 2026-07-28)

    def from_client(self, line: bytes) -> bytes | None:
        """Watch a line to the server. Returns the answer when a fault answers instead."""
        message = parse(line)
        if message.get("method") == "tools/list" and "id" in message:
            self.listing.add(key(message["id"]))
        if message.get("method") != "tools/call" or "id" not in message:
            return None
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        pending = Pending(
            name=str(params.get("name", "?")),
            arguments=json.dumps(params.get("arguments", {})),
            started=time.time_ns(),
            clock=time.monotonic(),
        )
        self.counts[pending.name] = self.counts.get(pending.name, 0) + 1
        request = ToolRequest(
            message["id"],
            pending.name,
            params.get("arguments", {}),
            self.counts[pending.name],
            ToolSession(self.schemas.get(pending.name), self.tags_results),
        )
        for injector in self.injectors.get(pending.name, []):
            spec = injector.fault.spec
            try:
                injection = injector.request(request)
            except FaultShapeError as exc:
                # Sent anyway, so the run goes on, and marked so the trace says why.
                print(
                    f"faultline mcp-wrap: {spec} does not fit the output schema of "
                    f"{pending.name} ({exc}); the client may reject it",
                    file=sys.stderr,
                )
                return self.answered(pending, exc.answer, spec, SHAPE_INVALID)
            if injection is not None and injection.answer is not None:
                return self.answered(pending, injection.answer, spec)
        self.pending[key(message["id"])] = pending
        return None

    def answered(
        self,
        pending: Pending,
        answer: dict,
        spec: str,
        error_type: str | None = None,
    ) -> bytes:
        """Record a call a fault answered, and return the answer's line."""
        result, kind = outcome(answer)
        self.finish(pending, result, error_type or kind, spec)
        return (json.dumps(answer) + "\n").encode()

    def from_server(self, line: bytes) -> None:
        message = parse(line)
        if "id" not in message or not ("result" in message or "error" in message):
            return
        result = message.get("result")
        if isinstance(result, dict):
            self.tags_results = self.tags_results or "resultType" in result
            if key(message["id"]) in self.listing:
                self.learn_schemas(result)
        self.listing.discard(key(message["id"]))
        pending = self.pending.pop(key(message["id"]), None)
        if pending is None:
            return  # the answer to a request that is not a tool call
        result, error_type = outcome(message)
        self.finish(pending, result, error_type)

    def learn_schemas(self, listed: dict) -> None:
        tools = listed.get("tools")
        for tool in tools if isinstance(tools, list) else []:
            if isinstance(tool, dict) and isinstance(tool.get("outputSchema"), dict):
                self.schemas[str(tool.get("name"))] = tool["outputSchema"]

    def finish(
        self,
        pending: Pending,
        result: str,
        error_type: str | None,
        injected: str | None = None,
    ) -> None:
        call = ToolCall(
            name=pending.name,
            call_id="",  # the model's id for the call is not in the MCP request
            arguments=pending.arguments,
            seconds=time.monotonic() - pending.clock,
            result=result,
            error_type=error_type,
            started=pending.started,
            injected=injected,
        )
        self.calls.append(call)
        if self.on_call:
            self.on_call(call)

    def close(self) -> None:
        """Record the calls the server never answered."""
        for pending in self.pending.values():
            self.finish(pending, "", "no_response")
        self.pending.clear()


def parse(line: bytes) -> dict:
    """One JSON-RPC message. A line that is not a JSON object gives an empty dict."""
    try:
        message = json.loads(line)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def key(request_id: object) -> str:
    return json.dumps(request_id)  # keeps the id 1 apart from the id "1"


def outcome(message: dict) -> tuple[str, str | None]:
    """The text of a tools/call response, and its error type when it failed."""
    if "error" in message:
        error = message["error"] if isinstance(message["error"], dict) else {}
        return str(error.get("message", "")), "mcp_error"
    result = message["result"] if isinstance(message["result"], dict) else {}
    content = result.get("content")
    blocks = content if isinstance(content, list) else []
    text = "\n".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return text, "tool_error" if result.get("isError") else None


async def relay(command: Sequence[str], calls: ToolCalls) -> int:
    """Run the server and relay lines both ways until it exits. Returns its exit code."""
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=LINE_LIMIT)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    stdout = asyncio.StreamWriter(transport, protocol, None, loop)
    signals = Relay()
    for number in RELAYED:
        loop.add_signal_handler(number, signals.receive, number)
    try:
        try:
            server = await asyncio.create_subprocess_exec(
                *tied_to_parent(command, "faultline mcp-wrap"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except OSError as exc:
            print(
                f"faultline mcp-wrap: cannot start {command[0]}: {exc}", file=sys.stderr
            )
            return 127 if isinstance(exc, FileNotFoundError) else 126
        signals.attach(server)
        assert server.stdin and server.stdout

        async def up() -> None:
            assert server.stdin
            try:
                while line := await stdin.readline():
                    if answer := calls.from_client(line):
                        stdout.write(answer)  # the server never sees this call
                        await stdout.drain()
                        continue
                    server.stdin.write(line)
                    await server.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server is gone. Its exit code says why.
            finally:
                server.stdin.close()

        async def down() -> None:
            assert server.stdout and server.stdin
            try:
                while line := await server.stdout.readline():
                    calls.from_server(line)
                    stdout.write(line)
                    await stdout.drain()
            except (BrokenPipeError, ConnectionResetError):
                server.stdin.close()  # the client is gone, so the server may stop

        upward = asyncio.create_task(up())
        await down()  # until the server closes its stdout
        code = await server.wait()
        upward.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await upward
    finally:
        signals.close()
        calls.close()  # calls never answered are still recorded
        for number in RELAYED:
            loop.remove_signal_handler(number)
    return 128 - code if code < 0 else code
