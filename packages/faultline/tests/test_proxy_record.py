"""The proxy's own trace, read back by the same reader that reads a team's traces."""

import gzip
import json
import subprocess
import sys
from pathlib import Path

from upstream import ANSWERS, ASKS, upstream_server

from faultline.model import ToolCall
from faultline.proxy.record import Exchange, Recorder
from faultline.runs import read_runs, read_runs_many

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")
TRACE_ID = "ab" * 16
ROOT = "cd" * 8


QUESTION = {"role": "user", "content": "Why does the cart fail?"}


def exchange(step: int, attempt: int, request: dict, status: int, reply: bytes, **kw):
    started = 1_000_000_000 * (10 * step + attempt)
    return Exchange(
        step=step,
        attempt=attempt,
        path=kw.pop("path", "/chat/completions"),
        request=json.dumps(request).encode(),
        status=status,
        response=reply,
        started=started,
        ended=started + 500_000_000,
        **kw,
    )


def recorder(tmp_path: Path, name: str = "model.jsonl") -> Recorder:
    return Recorder(tmp_path / name, TRACE_ID, ROOT)


def test_a_proxied_openai_run_reads_back_as_steps_with_tokens_and_an_answer(tmp_path):
    record = recorder(tmp_path)
    first = {"model": "tiny-model", "messages": [QUESTION]}
    second = {"model": "tiny-model", "messages": [QUESTION, {"role": "assistant"}]}

    record.exchange(exchange(1, 1, first, 200, ASKS))
    record.exchange(exchange(2, 1, second, 200, ANSWERS))
    record.flush()
    record.root(0, 30_000_000_000, ["python", "agent.py"], 0)

    [run] = read_runs(record.path).runs
    assert run.trace_id == TRACE_ID
    assert run.complete and run.error_type is None
    assert run.model == "tiny-model"
    assert run.question == "Why does the cart fail?"
    assert [(s.input_tokens, s.output_tokens, s.finish_reason) for s in run.steps] == [
        (50, 7, "tool_call"),
        (50, 7, "stop"),
    ]
    assert run.answer == "cart is down"


def test_an_anthropic_reply_reads_back_with_cached_input_counted(tmp_path):
    record = recorder(tmp_path)
    request = {
        "model": "claude",
        "system": "be brief",
        "messages": [{"role": "user", "content": "Why?"}],
    }
    reply = {
        "type": "message",
        "content": [{"type": "text", "text": "No fault found."}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 90,
            "output_tokens": 5,
        },
    }

    record.exchange(
        exchange(1, 1, request, 200, json.dumps(reply).encode(), path="/v1/messages")
    )
    record.flush()

    [run] = read_runs(record.path).runs
    [step] = run.steps
    assert (step.input_tokens, step.output_tokens, step.finish_reason) == (
        100,
        5,
        "stop",
    )
    assert run.question == "Why?"
    assert run.answer == "No fault found."


def test_a_gzipped_reply_is_read_for_the_trace(tmp_path):
    record = recorder(tmp_path)

    record.exchange(
        exchange(
            1, 1, {"messages": [QUESTION]}, 200, gzip.compress(ANSWERS), encoding="gzip"
        )
    )
    record.flush()

    assert read_runs(record.path).runs[0].answer == "cart is down"


def test_retries_of_one_step_are_one_span_with_every_attempt(tmp_path):
    record = recorder(tmp_path)
    error = b'{"error": {"code": "rate_limit_exceeded"}}'

    for attempt in (1, 2, 3):
        record.exchange(
            exchange(
                1,
                attempt,
                {"messages": [QUESTION]},
                429,
                error,
                injected="model:rate_limit",
            )
        )
    record.flush()

    lines = record.path.read_text().splitlines()
    span = json.loads(lines[0])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attributes = {a["key"]: a["value"] for a in span["attributes"]}
    assert len(lines) == 1
    assert attributes["faultline.proxy.attempts"] == {"intValue": "3"}
    assert [e["name"] for e in span["events"]] == ["faultline.proxy.attempt"] * 3
    [run] = read_runs(record.path).runs
    [step] = run.steps
    assert step.error_type == "429"
    assert step.injected == "model:rate_limit"
    assert run.answer is None


def test_spans_of_one_trace_in_several_files_join_into_one_run(tmp_path):
    model, tools = recorder(tmp_path), recorder(tmp_path, "mcp-1.jsonl")
    model.exchange(exchange(1, 1, {"messages": [QUESTION]}, 200, ASKS))
    model.exchange(exchange(2, 1, {"messages": [QUESTION, {}]}, 200, ANSWERS))
    model.flush()
    tools.tool_call(
        ToolCall(
            name="get_logs",
            call_id="",
            arguments='{"service": "cart"}',
            seconds=0.5,
            result="",
            started=12_000_000_000,  # after step 1 began, before step 2
            injected="tool:get_logs:empty",
        )
    )

    [run] = read_runs_many([model.path, tools.path]).runs

    [call] = run.steps[0].tool_calls
    assert call.name == "get_logs"
    assert call.injected == "tool:get_logs:empty"
    assert run.steps[1].tool_calls == ()


def test_faultline_run_writes_a_trace_that_python_m_faultline_runs_reads(tmp_path):
    trace = tmp_path / "trace"
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        agent = subprocess.run(
            [FAULTLINE, "run", "--upstream", upstream.url, "--trace", str(trace)]
            + [
                "--",
                sys.executable,
                str(HERE / "tiny_agent.py"),
                str(tmp_path / "log"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    shown = subprocess.run(
        [sys.executable, "-m", "faultline.runs", str(trace)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout

    assert agent.returncode == 0, agent.stderr
    assert agent.stdout == "cart is down\n"
    files = sorted(p.name for p in trace.iterdir())
    assert files[0].startswith("mcp-") and files[1:] == ["model.jsonl"]
    [run] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert run.complete
    assert [len(step.tool_calls) for step in run.steps] == [1, 0]
    assert run.steps[0].tool_calls[0].result == 'echo {"service": "cart"}'
    assert f"{trace}: 1 run(s)" in shown
    assert "steps=2" in shown and "complete" in shown
    assert 'tool get_logs({"service": "cart"}) ok' in shown
    assert "answer: cart is down" in shown
