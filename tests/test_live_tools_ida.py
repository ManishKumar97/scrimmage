"""No run reaches a live tool by default: the lab's IDA, its MCP servers wrapped.

Without --allow-live-tools, faultline lists the tools that would run for real and
stops the IDA before any does. The model is the lab's scripted fake_llm.
"""

import json
import sys
from pathlib import Path

from ida_harness import SCRIPT, fake_llm, ida, ida_env, needs_cluster, run

from faultline.runs import read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")


def ida_run(tmp_path: Path, *flags: str):
    trace = tmp_path / "trace"
    env = ida_env(
        tmp_path, MCP_COMMAND_PREFIX=json.dumps([FAULTLINE, "mcp-wrap", "--"])
    )
    with fake_llm(tmp_path, SCRIPT) as upstream:
        result = run(
            [FAULTLINE, "run", *flags, "--base-url-env", "GROQ_BASE_URL"]
            + ["--upstream", upstream, "--trace", str(trace), "--", *ida()],
            env,
            tmp_path,
        )
    return result, trace


def test_without_the_flag_the_tools_are_listed_and_none_runs(tmp_path):
    result, trace = ida_run(tmp_path)

    assert result.returncode == 2, result.stderr
    out = result.stderr[result.stderr.index("faultline run: a preview.") :]
    python = sys.executable
    # Every tool of both MCP servers, grouped by server.
    assert out.splitlines() == [
        (
            "faultline run: a preview. No MCP tool under faultline mcp-wrap ran: "
            "every call was blocked, and every model call was held."
        ),
        "These MCP tools would run for real:",
        f"  {python} -m faultline_lab.mcp_logs:",
        "    get_logs",
        f"  {python} -m faultline_lab.mcp_metrics:",
        "    get_service_metrics",
        "    get_error_rate_trend",
        (
            "Tools in the agent's own code (function tools) never pass through "
            "faultline, so they are not listed. Check them yourself."
        ),
        *(
            f"{name} was not set, so the agent's calls through it bypass "
            "faultline and were not held."
            for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")
        ),
        "Pass --allow-live-tools to run with these tools.",
    ]
    # No tool call was made, and the model call was held: the fake model got nothing.
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert read.tool_calls == ()
    assert [step.error_type for step in read.steps] == ["held_for_tool_list"]
    assert "[tool]" not in result.stderr


@needs_cluster
def test_with_the_flag_the_run_completes(tmp_path):
    result, trace = ida_run(tmp_path, "--allow-live-tools", "--expect", "cart")

    assert result.returncode == 0, result.stderr
    assert '[tool] get_logs({"service": "cart"})\n' in result.stderr
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    [call] = read.tool_calls
    assert (call.name, call.error_type) == ("get_logs", None)
    assert result.stderr.splitlines()[-1] == (
        "verdict: passed (answer matches the expected answer)"
    )
