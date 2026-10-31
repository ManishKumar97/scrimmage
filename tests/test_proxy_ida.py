"""The IDA through the proxy, model and MCP, gives the same diagnosis as without it."""

import asyncio
import json
import sys
import threading
from pathlib import Path

from ida_harness import QUESTION, SCRIPT, fake_llm, ida, ida_env, needs_cluster, run

from faultline.model import ToolCall
from faultline.proxy.model import ModelProxy
from faultline.runs import read_runs, read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")


def tool_calls(trace_file: Path) -> list[ToolCall]:
    """The tool calls the IDA recorded in its own trace."""
    return [call for run in read_runs(trace_file).runs for call in run.tool_calls]


def shape(call: ToolCall) -> tuple:
    """What two runs a few seconds apart must share: the call, its outcome, and the
    pods the logs came from. The log lines themselves move on between the runs."""
    pods = sorted(
        line for line in call.result.splitlines() if line.startswith("--- pod")
    )
    return call.name, call.arguments, call.error_type, pods


@needs_cluster
def test_the_ida_answers_the_same_through_the_model_proxy(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with fake_llm(tmp_path) as upstream:
        proxy = ModelProxy({"GROQ_BASE_URL": upstream})
        asyncio.run_coroutine_threadsafe(proxy.start(), loop).result(5)
        url = proxy.url_for("GROQ_BASE_URL")
        try:
            direct = run(
                ida(),
                ida_env(
                    tmp_path, GROQ_BASE_URL=upstream, TRACE_FILE=str(tmp_path / "a")
                ),
                tmp_path,
            )
            proxied = run(
                ida(),
                ida_env(tmp_path, GROQ_BASE_URL=url, TRACE_FILE=str(tmp_path / "b")),
                tmp_path,
            )
        finally:
            asyncio.run_coroutine_threadsafe(proxy.stop(), loop).result(5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()

    assert direct.returncode == 0, direct.stderr
    assert "[stop] done after 2 steps" in direct.stderr
    assert "[tool] get_logs" in direct.stderr
    assert proxied.returncode == 0, proxied.stderr
    assert proxied.stdout == direct.stdout
    assert "ROOT CAUSE SERVICE: cart" in proxied.stdout
    assert proxy.steps.number == 2  # both model calls went through the proxy
    seen_direct, seen_proxied = tool_calls(tmp_path / "a"), tool_calls(tmp_path / "b")
    assert [shape(c) for c in seen_proxied] == [shape(c) for c in seen_direct]
    assert seen_proxied[0].result.startswith("Now: ")  # real logs, not an error


@needs_cluster
def test_the_ida_answers_the_same_with_its_mcp_servers_wrapped(tmp_path):
    wrap = json.dumps([FAULTLINE, "mcp-wrap", "--"])
    with fake_llm(tmp_path) as upstream:
        plain = run(
            ida(),
            ida_env(tmp_path, GROQ_BASE_URL=upstream, TRACE_FILE=str(tmp_path / "a")),
            tmp_path,
        )
        wrapped = run(
            ida(),
            ida_env(
                tmp_path,
                GROQ_BASE_URL=upstream,
                MCP_COMMAND_PREFIX=wrap,
                TRACE_FILE=str(tmp_path / "b"),
            ),
            tmp_path,
        )

    assert plain.returncode == 0, plain.stderr
    assert wrapped.returncode == 0, wrapped.stderr
    assert "[tool] get_logs" in wrapped.stderr
    assert "-> error" not in wrapped.stderr
    assert wrapped.stdout == plain.stdout
    seen_plain, seen_wrapped = tool_calls(tmp_path / "a"), tool_calls(tmp_path / "b")
    assert [shape(c) for c in seen_wrapped] == [shape(c) for c in seen_plain]
    assert seen_wrapped[0].result.startswith("Now: ")


def under_faultline(upstream: str, trace: Path, *flags: str) -> list[str]:
    return [
        FAULTLINE,
        "run",
        "--allow-live-tools",
        "--base-url-env",
        "GROQ_BASE_URL",
        "--upstream",
        upstream,
        "--trace",
        str(trace),
        *flags,
        "--",
        *ida(),
    ]


def wrapped_env(tmp_path: Path) -> dict[str, str]:
    prefix = json.dumps([FAULTLINE, "mcp-wrap", "--"])
    # No MCP_PASS_ENV: the wrapper finds the run from its parent processes.
    return ida_env(tmp_path, MCP_COMMAND_PREFIX=prefix)


@needs_cluster
def test_faultline_run_records_the_ida_run_as_one_trace(tmp_path):
    trace = tmp_path / "trace"
    with fake_llm(tmp_path) as upstream:
        result = run(under_faultline(upstream, trace), wrapped_env(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    [run_read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert run_read.complete
    assert run_read.question == QUESTION
    assert run_read.answer == SCRIPT[1]["text"]
    assert result.stdout == f"{run_read.answer}\n"
    [call] = run_read.steps[0].tool_calls
    assert (call.name, call.error_type, call.injected) == ("get_logs", None, None)
    assert call.result.startswith("Now: ")  # real log lines from the cluster
    # The wrapper relayed the result unchanged: the server's text, as the IDA got it.
    [ida_saw] = tool_calls(tmp_path / "ida.jsonl")
    assert ida_saw.result == call.result
    assert run_read.steps[1].tool_calls == ()
