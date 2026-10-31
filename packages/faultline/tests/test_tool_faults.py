"""Tool faults in `faultline mcp-wrap`, run as `faultline run` starts it, on a stub server."""

import json
import os
import subprocess
import sys
from pathlib import Path

from faultline.proxy.context import RunContext

FAULTLINE = str(Path(sys.executable).parent / "faultline")
SERVER = str(Path(__file__).parent / "stdio_server.py")
TRACE_ID, ROOT = "ab" * 16, "cd" * 8


def line(message: dict) -> bytes:
    return (json.dumps(message) + "\n").encode()


def call(request_id: object, name: str, **arguments) -> bytes:
    params = {"name": name, "arguments": arguments}
    return line(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    )


# A session as an MCP client starts one, then three tool calls.
OPENING = [
    line({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}),
    line({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    line({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
]


def wrapped(
    tmp_path: Path, sent: list[bytes], faults: list[str], timeout: float = 30
) -> tuple[list[bytes], bytes, list[dict]]:
    """The lines the client got, the bytes the server got, and the run's trace."""
    trace = tmp_path / "trace"
    log = tmp_path / "server.log"
    log.unlink(missing_ok=True)
    env = {**os.environ, **RunContext(str(trace), TRACE_ID, ROOT, tuple(faults)).env()}
    result = subprocess.run(
        [FAULTLINE, "mcp-wrap", "--", sys.executable, SERVER, str(log), "0"],
        input=b"".join(sent),
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines(keepends=True), log.read_bytes(), spans(trace)


def spans(trace: Path) -> list[dict]:
    """The wrapper's execute_tool spans in call order, each as a dict of attributes."""
    found = []
    for path in sorted(trace.glob("mcp-*.jsonl")):
        for text in path.read_text().splitlines():
            for resource in json.loads(text)["resourceSpans"]:
                for scope in resource["scopeSpans"]:
                    found += scope["spans"]
    found.sort(key=lambda span: int(span["startTimeUnixNano"]))
    return [
        {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}
        for span in found
    ]


def direct(tmp_path: Path, sent: list[bytes]) -> list[bytes]:
    """The lines the server alone answers with, no wrapper in between."""
    result = subprocess.run(
        [sys.executable, SERVER, str(tmp_path / "direct.log"), "0"],
        input=b"".join(sent),
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout.splitlines(keepends=True)


def test_a_tool_error_answers_the_chosen_call_and_no_other_byte_changes(tmp_path):
    targeted = call(3, "get_logs", service="cart")
    sent = [
        *OPENING,
        call(2, "get_logs", service="cart"),
        call("m", "get_metrics", service="cart"),
        targeted,
        b"not json at all\n",
        call(4, "get_logs", service="ad"),
    ]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:error@2"])

    # The server got every byte but the targeted call's, and answered as it would alone.
    assert server_saw == b"".join(line for line in sent if line != targeted)
    [injected] = [line for line in got if json.loads(line).get("id") == 3]
    assert json.loads(injected)["result"] == {
        "content": [
            {
                "type": "text",
                "text": "Error executing tool get_logs: the backend did not respond "
                "(connection reset by peer)",
            }
        ],
        "isError": True,
    }
    assert [line for line in got if line != injected] == direct(
        tmp_path, [line for line in sent if line != targeted]
    )
    assert [
        (s["gen_ai.tool.name"], s.get("error.type"), s.get("faultline.injected"))
        for s in recorded
    ] == [
        ("get_logs", None, None),
        ("get_metrics", None, None),
        ("get_logs", "tool_error", "tool:get_logs:error@2"),
        ("get_logs", None, None),
    ]


def cancel(request_id: object) -> bytes:
    params = {"requestId": request_id, "reason": "timed out after 2.0s"}
    return line(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
    )


def test_a_tool_timeout_never_answers_the_chosen_call(tmp_path):
    held = call(3, "get_logs", service="cart")
    sent = [
        *OPENING,
        call(2, "get_logs", service="cart"),
        held,
        call(4, "get_metrics", service="cart"),
        cancel(3),  # the client gave up on call 3
    ]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:timeout@2"])

    # Neither the call nor its cancel reached the server, and no answer came back.
    assert server_saw == b"".join(sent[:4] + sent[5:6])
    assert [json.loads(line)["id"] for line in got] == [0, 1, 2, 4]
    assert [
        (s["gen_ai.tool.name"], s.get("error.type"), s.get("faultline.injected"))
        for s in recorded
    ] == [
        ("get_logs", None, None),
        ("get_logs", "timeout", "tool:get_logs:timeout@2"),
        ("get_metrics", None, None),
    ]


def test_a_held_call_never_cancelled_is_a_timeout_when_the_server_ends(tmp_path):
    sent = [*OPENING, call(2, "get_logs", service="cart")]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:timeout"])

    assert server_saw == b"".join(OPENING)
    assert [json.loads(line)["id"] for line in got] == [0, 1]
    assert [(s.get("error.type"), s.get("faultline.injected")) for s in recorded] == [
        ("timeout", "tool:get_logs:timeout")
    ]
