"""The MCP wrapper: lines pass both ways unchanged, and tool calls are paired by id."""

import json
import subprocess
import sys
from pathlib import Path

from faultline.faults import ToolFault
from faultline.proxy.mcp import ToolCalls

FAULTLINE = str(Path(sys.executable).parent / "faultline")
SERVER = str(Path(__file__).parent / "stdio_server.py")


def line(message: dict) -> bytes:
    return (json.dumps(message) + "\n").encode()


def wrapped(sent: bytes, tmp_path: Path, exit_code: int = 0):
    log = tmp_path / "server.log"
    command = [
        FAULTLINE,
        "mcp-wrap",
        "--",
        sys.executable,
        SERVER,
        str(log),
        str(exit_code),
    ]
    result = subprocess.run(
        command, input=sent, capture_output=True, timeout=30, check=False
    )
    return result, log.read_bytes()


def test_lines_pass_both_ways_unchanged_and_stderr_passes_through(tmp_path):
    sent = (
        b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b'{ "jsonrpc" : "2.0", "id" : "a", "method" : "tools/call",'
        b' "params" : {"name": "get_logs", "arguments": {"service": "caf\xc3\xa9"}}}\n'
        b"not json at all\n"
    )

    result, received = wrapped(sent, tmp_path)

    assert received == sent
    assert result.stdout == (
        b'{"jsonrpc": "2.0",   "id": 0, "result": {}}\n'
        b'{"jsonrpc": "2.0",   "id": "a", "result": {"content": [{"type": "text",'
        b' "text": "echo {\\"service\\": \\"caf\xc3\xa9\\"}"}], "isError": false}}\n'
    )
    assert b"stub server started" in result.stderr
    assert result.returncode == 0


def test_the_servers_exit_code_is_the_wrappers(tmp_path):
    result, _ = wrapped(b"", tmp_path, exit_code=5)

    assert result.returncode == 5


def test_a_large_result_line_passes_whole(tmp_path):
    big = "x" * 3_000_000  # more than asyncio's default 64 KiB line limit
    sent = line(
        {
            "id": 1,
            "method": "tools/call",
            "params": {"name": "t", "arguments": {"a": big}},
        }
    )

    result, received = wrapped(sent, tmp_path)

    assert received == sent
    assert big in result.stdout.decode()


def call(request_id: object, name: str, **arguments) -> bytes:
    params = {"name": name, "arguments": arguments}
    return line(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    )


def answer(request_id: object, text: str, is_error: bool = False) -> bytes:
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    return line({"jsonrpc": "2.0", "id": request_id, "result": result})


def test_each_tool_call_is_paired_with_the_response_of_the_same_id():
    calls = ToolCalls()

    calls.from_client(call(1, "get_logs", service="cart"))
    calls.from_client(call("1", "get_service_metrics", service="cart"))
    calls.from_client(line({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    calls.from_server(line({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}))
    calls.from_server(answer("1", "rate 0.5"))
    calls.from_server(answer(1, "line one"))

    assert [(c.name, c.arguments, c.result, c.error_type) for c in calls.calls] == [
        ("get_service_metrics", '{"service": "cart"}', "rate 0.5", None),
        ("get_logs", '{"service": "cart"}', "line one", None),
    ]
    assert all(c.started > 0 and c.seconds >= 0 for c in calls.calls)


def test_failed_and_unanswered_tool_calls_are_errors():
    calls = ToolCalls()

    calls.from_client(call(1, "fail"))
    calls.from_client(call(2, "bad"))
    calls.from_client(call(3, "hang"))
    calls.from_server(answer(1, "it broke", is_error=True))
    calls.from_server(
        line(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32602, "message": "no such tool"},
            }
        )
    )
    calls.close()

    assert [(c.name, c.result, c.error_type) for c in calls.calls] == [
        ("fail", "it broke", "tool_error"),
        ("bad", "no such tool", "mcp_error"),
        ("hang", "", "no_response"),
    ]


def test_a_faked_result_takes_the_shape_the_server_taught_the_wrapper():
    calls = ToolCalls(faults=[ToolFault("tool:get_logs:empty", "get_logs", "empty")])
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    listed = {"tools": [{"name": "get_logs", "outputSchema": schema}]}

    calls.from_client(line({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    calls.from_server(
        line(
            {"jsonrpc": "2.0", "id": 1, "result": {**listed, "resultType": "complete"}}
        )
    )
    answer = calls.from_client(call(2, "get_logs", service="cart"))

    assert answer is not None
    assert json.loads(answer) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [],
            "isError": False,
            "structuredContent": {"result": ""},
            "resultType": "complete",
        },
    }
    [recorded] = calls.calls
    assert (recorded.result, recorded.injected) == ("", "tool:get_logs:empty")
