"""Tool faults on the lab's IDA: its real MCP servers run under faultline mcp-wrap."""

import json
import sys
from pathlib import Path

from ida_harness import SCRIPT, fake_llm, ida, ida_env, needs_cluster, run

from faultline.model import ToolCall
from faultline.runs import read_runs, read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")


def ida_run(tmp_path: Path, script: list[dict], *flags: str, **settings: str):
    """The IDA under faultline run, its MCP servers wrapped. Returns the result,
    the tool calls the IDA saw and the run as the proxy recorded it."""
    trace = tmp_path / "trace"
    env = ida_env(
        tmp_path,
        MCP_COMMAND_PREFIX=json.dumps([FAULTLINE, "mcp-wrap", "--"]),
        **settings,
    )
    with fake_llm(tmp_path, script) as upstream:
        result = run(
            [FAULTLINE, "run", *flags, "--base-url-env", "GROQ_BASE_URL"]
            + ["--upstream", upstream, "--trace", str(trace), "--", *ida()],
            env,
            tmp_path,
        )
    seen: list[ToolCall] = [
        c for r in read_runs(tmp_path / "ida.jsonl").runs for c in r.tool_calls
    ]
    runs = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    return result, seen, runs[0] if runs else None


@needs_cluster
def test_the_ida_gets_an_error_from_the_chosen_tool(tmp_path):
    result, seen, read = ida_run(
        tmp_path, SCRIPT, "--fault", "tool:get_logs:error", "--expect", "cart"
    )

    assert result.returncode == 0, result.stderr
    assert '[tool] get_logs({"service": "cart"}) -> error\n' in result.stderr
    [call] = seen
    assert call.error_type == "tool_error"
    assert call.result == (
        "Error: get_logs raised RuntimeError: Error executing tool get_logs: "
        "the backend did not respond (connection reset by peer)"
    )
    assert read is not None
    [recorded] = read.tool_calls
    assert (recorded.error_type, recorded.injected) == (
        "tool_error",
        "tool:get_logs:error",
    )
