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


@needs_cluster
def test_a_chosen_tool_call_hangs_until_the_idas_own_timeout_fires(tmp_path):
    result, seen, read = ida_run(
        tmp_path,
        SCRIPT,
        "--fault",
        "tool:get_logs:timeout",
        "--expect",
        "cart",
        TOOL_TIMEOUT="2",
    )

    assert result.returncode == 0, result.stderr
    assert '[tool] get_logs({"service": "cart"}) -> error\n' in result.stderr
    [call] = seen
    assert (
        call.result == "Error: get_logs raised MCPError: Request 'tools/call' timed out"
    )
    assert call.seconds >= 2  # the IDA waited out its own timeout
    assert read is not None
    [recorded] = read.tool_calls
    assert (recorded.error_type, recorded.injected) == (
        "timeout",
        "tool:get_logs:timeout",
    )


@needs_cluster
def test_the_ida_gets_the_result_an_earlier_run_got_as_a_success(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    result, [then], _ = ida_run(first, SCRIPT)
    assert result.returncode == 0, result.stderr

    result, [now], read = ida_run(
        second,
        SCRIPT,
        "--fault",
        "tool:get_logs:stale",
        "--stale-from",
        str(first / "trace"),
    )

    assert result.returncode == 0, result.stderr
    assert '[tool] get_logs({"service": "cart"})\n' in result.stderr  # no error
    # The logs say the time they were read: the IDA got the earlier run's logs.
    assert (now.result, now.error_type) == (then.result, None)
    assert read is not None
    [recorded] = read.tool_calls
    assert recorded.injected == "tool:get_logs:stale"


@needs_cluster
def test_the_ida_gets_logs_that_name_the_wrong_service_as_a_success(tmp_path):
    result, [seen], read = ida_run(
        tmp_path, SCRIPT, "--fault", "tool:get_logs:wrong=payment"
    )

    assert result.returncode == 0, result.stderr
    assert '[tool] get_logs({"service": "cart"})\n' in result.stderr  # no error
    # The IDA asked about cart and got well-formed logs of pods named payment.
    assert seen.error_type is None
    assert "cart" not in seen.result
    assert "--- pod payment-" in seen.result
    assert read is not None
    [recorded] = read.tool_calls
    assert (recorded.result, recorded.injected) == (
        seen.result,
        "tool:get_logs:wrong=payment",
    )
    spans = (tmp_path / "trace").glob("mcp-*.jsonl")
    assert any('"faultline.injected.value"' in p.read_text() for p in spans)
