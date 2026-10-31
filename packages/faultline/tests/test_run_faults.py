"""`faultline run --fault ...` end to end, with the tiny agent and stand-in servers."""

import json
import subprocess
import sys
from pathlib import Path

from sse import UNKNOWN_STREAM
from upstream import ANSWERS, ASKS, upstream_server

from faultline.runs import read_runs_many

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")

# One model call. Prints the status and Retry-After it got, as an agent's code sees them.
ONE_CALL = """
import os, urllib.error, urllib.request
request = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/chat/completions", b"{}")
try:
    urllib.request.urlopen(request)
except urllib.error.HTTPError as error:
    print(error.code, error.headers["Retry-After"], error.read().decode())
"""


def faultline_run(tmp_path: Path, upstream: str, *args: str):
    trace = tmp_path / "trace"
    result = subprocess.run(
        [
            FAULTLINE,
            "run",
            "--allow-live-tools",
            "--upstream",
            upstream,
            "--trace",
            str(trace),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, read_runs_many(sorted(trace.glob("*.jsonl"))).runs


def test_an_empty_tool_result_is_answered_by_the_wrapper_and_judged(tmp_path):
    log = tmp_path / "server.log"
    agent = [sys.executable, str(HERE / "tiny_agent.py"), str(log)]
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        result, [run] = faultline_run(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_logs:empty",
            "--expect",
            "checkout",
            "--",
            *agent,
        )
        tool_message = json.loads(upstream.seen[1][3])["messages"][-1]

    assert result.returncode == 0, result.stderr
    assert log.read_bytes() == b""  # the server never got the call
    assert tool_message == {"role": "tool", "tool_call_id": "call_1", "content": ""}
    [call] = run.steps[0].tool_calls
    assert (call.result, call.error_type) == ("", None)
    assert call.injected == "tool:get_logs:empty"
    assert result.stderr.splitlines()[-1] == (
        'verdict: failed_silently (answer lacks "checkout", compared with the '
        "expected answer, and says nothing of its own work falling short (heuristic); injected tool:get_logs:empty)"
    )


def test_a_rate_limit_answers_the_step_without_reaching_the_upstream(tmp_path):
    with upstream_server() as upstream:
        result, [run] = faultline_run(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit",
            "--",
            sys.executable,
            "-c",
            ONE_CALL,
        )
        seen = upstream.seen

    assert seen == []
    status, retry_after, body = result.stdout.split(" ", 2)
    assert (status, retry_after) == ("429", "1")
    assert json.loads(body)["error"]["code"] == "rate_limit_exceeded"
    assert run.steps[0].error_type == "429"
    assert run.steps[0].injected == "model:rate_limit"
    assert result.stderr.splitlines()[-1] == (
        "verdict: failed_loudly (the last model call failed: 429; "
        "injected model:rate_limit)"
    )


def test_a_rate_limit_on_a_later_step_lets_the_earlier_ones_through(tmp_path):
    with upstream_server() as upstream:
        upstream.replies = [ASKS]
        result, [run] = faultline_run(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit@2",
            "--",
            sys.executable,
            str(HERE / "tiny_agent.py"),
            str(tmp_path / "log"),
        )

    assert result.returncode == 1  # the agent did not handle the 429
    assert len(upstream.seen) == 1
    assert [s.error_type for s in run.steps] == [None, "429"]
    assert result.stderr.splitlines()[-1] == (
        "verdict: failed_loudly (exit code 1; injected model:rate_limit@2)"
    )


def test_an_unknown_fault_stops_before_the_agent_starts(tmp_path):
    result, runs = faultline_run(
        tmp_path, "http://x", "--fault", "tool:x:melt", "--", "echo", "ran"
    )

    assert result.returncode == 2
    assert "ran" not in result.stdout
    assert "unknown fault 'tool:x:melt'. Known: tool:NAME:empty" in result.stderr
    assert runs == []


def test_without_expect_a_finished_run_has_no_reference(tmp_path):
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        result, _ = faultline_run(
            tmp_path,
            upstream.url,
            "--",
            sys.executable,
            str(HERE / "tiny_agent.py"),
            str(tmp_path / "log"),
        )

    assert result.stderr.splitlines()[-1] == (
        "no reference: pass --expect or use faultline test"
    )


READS_A_STREAM = """
import os, urllib.request
request = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/chat/completions", b"{}")
print(len(urllib.request.urlopen(request).read()))
"""


def test_a_stream_the_proxy_cannot_read_is_no_verdict_not_a_failure(tmp_path):
    with upstream_server() as upstream:
        upstream.reply_headers = [("Content-Type", "text/event-stream")]
        upstream.chunks = [UNKNOWN_STREAM]
        result, [run] = faultline_run(
            tmp_path,
            upstream.url,
            "--expect",
            "cart",
            "--",
            sys.executable,
            "-c",
            READS_A_STREAM,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{len(UNKNOWN_STREAM)}\n"  # the agent got every byte
    assert run.answer is None
    assert result.stderr.splitlines()[-1] == (
        "no verdict: answer not recorded (streamed reply)"
    )


def test_a_tool_fault_without_a_wrapped_server_is_not_applied(tmp_path):
    with upstream_server() as upstream:
        result, _ = faultline_run(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_logs:empty",
            "--",
            sys.executable,
            "-c",
            ONE_CALL,
        )

    assert result.returncode == 78
    assert result.stderr.splitlines()[-2:] == [
        (
            "faultline run: fault tool:get_logs:empty was never applied "
            "(no MCP server ran under faultline mcp-wrap)"
        ),
        "verdict: not_applied (never applied: tool:get_logs:empty)",
    ]


def test_a_tool_fault_on_a_tool_never_called_is_not_applied(tmp_path):
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        result, _ = faultline_run(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_metrics:empty",
            "--fault",
            "model:rate_limit@5",
            "--",
            sys.executable,
            str(HERE / "tiny_agent.py"),
            str(tmp_path / "log"),
        )

    assert result.returncode == 78
    assert result.stderr.splitlines()[-3:] == [
        (
            "faultline run: fault tool:get_metrics:empty was never applied "
            "(the agent never called get_metrics; MCP tools called: get_logs)"
        ),
        (
            "faultline run: fault model:rate_limit@5 was never applied "
            "(the run made 2 model call(s), so there was no step 5)"
        ),
        (
            "verdict: not_applied (never applied: tool:get_metrics:empty, "
            "model:rate_limit@5)"
        ),
    ]


def test_a_server_started_with_a_cleaned_environment_still_gets_the_fault(tmp_path):
    log = tmp_path / "server.log"
    agent = [sys.executable, str(HERE / "tiny_agent.py"), str(log), "bare"]
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        result, [run] = faultline_run(
            tmp_path, upstream.url, "--fault", "tool:get_logs:empty", "--", *agent
        )

    assert result.returncode == 0, result.stderr
    assert log.read_bytes() == b""
    [call] = run.steps[0].tool_calls
    assert call.injected == "tool:get_logs:empty"
