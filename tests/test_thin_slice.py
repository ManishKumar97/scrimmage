"""The thin slice of Reproduce: a fault through the proxy, a trace, and a verdict."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ida_harness import QUESTION, fake_llm, ida, ida_env, needs_cluster, run

from faultline.runs import read_runs, read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")
TRACE_NAMES = [
    "FAULTLINE_TRACE_DIR",
    "FAULTLINE_TRACE_ID",
    "FAULTLINE_ROOT_SPAN_ID",
    "FAULTLINE_FAULTS",
]


def spans_of(trace: Path) -> list[dict]:
    lines = (trace / "model.jsonl").read_text().splitlines()
    return [
        span
        for line in lines
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def test_an_injected_429_reaches_the_sdk_agent_after_all_its_retries(tmp_path):
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    env["OPENAI_API_KEY"] = "fake"
    agent = [sys.executable, str(Path(__file__).parent / "openai_agent.py")]
    result = subprocess.run(
        [FAULTLINE, "run", "--fault", "model:rate_limit", "--trace", str(trace)]
        # Nothing listens here: the fault answers every attempt, so no call leaves.
        + ["--upstream", "http://127.0.0.1:9/v1", "--", *agent],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.stdout == "agent caught openai.RateLimitError: 429\n"
    assert result.returncode == 3
    chats = [s for s in spans_of(trace) if s["name"].startswith("chat")]
    assert len(chats) == 1  # one step
    attributes = {a["key"]: a["value"] for a in chats[0]["attributes"]}
    assert attributes["faultline.proxy.attempts"] == {"intValue": "3"}
    assert attributes["faultline.injected"] == {"stringValue": "model:rate_limit"}
    assert len(chats[0]["events"]) == 3
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [s.error_type for s in read.steps] == ["429"]
    assert result.stderr.splitlines()[-1] == (
        "verdict: failed_loudly (exit code 3; injected model:rate_limit)"
    )


# Step 1 asks for the logs of cart. With them empty, the scripted model finds nothing.
EMPTY_LOGS_SCRIPT = [
    {"tool_calls": [{"name": "get_logs", "arguments": {"service": "cart"}}]},
    {"text": "The logs show no errors. No fault found.\nROOT CAUSE SERVICE: none"},
]


@needs_cluster
def test_an_empty_tool_result_through_the_proxy_gets_a_verdict(tmp_path):
    trace = tmp_path / "trace"
    env = ida_env(
        tmp_path,
        MCP_COMMAND_PREFIX=json.dumps([FAULTLINE, "mcp-wrap", "--"]),
        MCP_PASS_ENV=json.dumps(TRACE_NAMES),
    )
    with fake_llm(tmp_path, EMPTY_LOGS_SCRIPT) as upstream:
        result = run(
            [FAULTLINE, "run", "--fault", "tool:get_logs:empty", "--expect", "cart"]
            + ["--base-url-env", "GROQ_BASE_URL", "--upstream", upstream]
            + ["--trace", str(trace), "--", *ida()],
            env,
            tmp_path,
        )

    assert result.returncode == 0, result.stderr
    # The agent got a success with nothing in it, not a protocol error.
    assert '[tool] get_logs({"service": "cart"})\n' in result.stderr
    [seen] = [c for r in read_runs(tmp_path / "ida.jsonl").runs for c in r.tool_calls]
    assert (seen.result, seen.error_type) == (
        "The tool returned an empty result.",
        None,
    )
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert read.question == QUESTION
    [call] = read.steps[0].tool_calls
    assert (call.name, call.result, call.injected) == (
        "get_logs",
        "",
        "tool:get_logs:empty",
    )
    assert result.stderr.splitlines()[-1] == (
        'verdict: failed_silently (answer lacks "cart"; injected tool:get_logs:empty)'
    )
