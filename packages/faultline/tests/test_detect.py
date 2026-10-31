import dataclasses
import json
from pathlib import Path

import pytest
from spans import run_from, write_trace

from faultline import cli
from faultline.config import DetectConfig
from faultline.detect import detect
from faultline.model import FaultKind, Origin, Run

CONFIG = DetectConfig()


def calls(*calls: dict) -> dict:
    return {"calls": list(calls)}


def faults_of(tmp_path: Path, *steps: dict):
    return detect(run_from(tmp_path, *steps), CONFIG)


def kinds_of(tmp_path: Path, *steps: dict) -> list[FaultKind]:
    return [fault.kind for fault in faults_of(tmp_path, *steps)]


def test_a_tool_span_that_ended_in_error_is_a_tool_error(tmp_path):
    step = calls({"id": "c1", "name": "get_logs", "error": "ConnectionError"})

    [fault] = faults_of(tmp_path, step)

    assert fault.kind is FaultKind.TOOL_ERROR
    assert (fault.step, fault.call_id, fault.tool) == (1, "c1", "get_logs")
    assert fault.detail == "ConnectionError"
    assert fault.origin is Origin.OBSERVED


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        ('{"error": "no such pod"}', "error: no such pod"),
        ('{"error": {"code": 500}, "data": 1}', "error: {'code': 500}"),
        ('{"isError": true, "content": "boom"}', "isError"),
    ],
)
def test_a_result_that_reports_an_error_is_a_tool_error(tmp_path, result, detail):
    [fault] = faults_of(tmp_path, calls({"result": result}))

    assert fault.kind is FaultKind.TOOL_ERROR
    assert fault.detail == detail


@pytest.mark.parametrize(
    "result",
    [
        "ok",
        "error: this is plain text, not a JSON object",
        '{"error": null, "data": 1}',
        '{"isError": false, "data": 1}',
        '["error"]',
        '"error"',
        "{broken json",
    ],
)
def test_a_result_that_is_not_an_error_is_not_a_tool_error(tmp_path, result):
    assert kinds_of(tmp_path, calls({"result": result})) == []


@pytest.mark.parametrize("error", ["TimeoutError", "ReadTimedOut", "tool timeout"])
def test_an_error_type_that_says_timeout_is_a_tool_timeout_and_not_an_error(
    tmp_path, error
):
    [fault] = faults_of(tmp_path, calls({"error": error}))

    assert fault.kind is FaultKind.TOOL_TIMEOUT
    assert fault.detail == error


def test_a_call_that_ran_past_the_limit_is_a_tool_timeout(tmp_path):
    [fault] = faults_of(tmp_path, calls({"seconds": 41.2}))

    assert fault.kind is FaultKind.TOOL_TIMEOUT
    assert fault.detail == "ran 41.2s, limit 30s"


def test_the_limit_comes_from_the_config(tmp_path):
    run = run_from(tmp_path, calls({"seconds": 5}))

    assert detect(run, DetectConfig(tool_timeout_seconds=10)) == ()
    [fault] = detect(run, DetectConfig(tool_timeout_seconds=2.5))
    assert fault.detail == "ran 5.0s, limit 2.5s"


def test_a_slow_call_that_failed_is_one_timeout_not_two_faults(tmp_path):
    step = calls({"seconds": 50, "error": "ConnectionError"})

    assert kinds_of(tmp_path, step) == [FaultKind.TOOL_TIMEOUT]


def test_a_call_at_the_limit_or_failing_fast_is_not_a_timeout(tmp_path):
    assert kinds_of(tmp_path, calls({"seconds": 30})) == []
    fast = calls({"seconds": 0.1, "error": "ConnectionError"})
    assert kinds_of(tmp_path, fast) == [FaultKind.TOOL_ERROR]


@pytest.mark.parametrize(
    "result",
    [
        "",
        "   \n",
        "null",
        '""',
        "[]",
        "{}",
        '{"items": []}',
        '{"a": null, "b": {}, "c": ""}',
    ],
)
def test_a_successful_call_with_nothing_in_the_result_is_an_empty_result(
    tmp_path, result
):
    [fault] = faults_of(tmp_path, calls({"id": "q", "name": "query", "result": result}))

    assert fault.kind is FaultKind.EMPTY_RESULT
    assert (fault.step, fault.call_id, fault.tool) == (1, "q", "query")
    assert fault.detail == (" ".join(result.split()) or "(blank)")


@pytest.mark.parametrize(
    "result",
    [
        "0",
        "false",
        "[0]",
        '{"count": 0}',
        '{"a": null, "b": 1}',
        '{"a": {"b": null}}',
        "none",
        "[]x",
    ],
)
def test_a_result_with_something_in_it_is_not_an_empty_result(tmp_path, result):
    assert kinds_of(tmp_path, calls({"result": result})) == []


def test_a_failed_call_with_no_result_is_an_error_not_an_empty_result(tmp_path):
    step = calls({"result": "", "error": "ConnectionError"})

    assert kinds_of(tmp_path, step) == [FaultKind.TOOL_ERROR]


def test_a_run_that_recorded_no_content_has_no_empty_results(tmp_path):
    run = run_from(tmp_path, calls({"result": ""}))
    unrecorded = dataclasses.replace(run, content_recorded=False)

    assert detect(run, CONFIG)[0].kind is FaultKind.EMPTY_RESULT
    assert detect(unrecorded, CONFIG) == ()


@pytest.mark.parametrize(
    "error", ["RateLimitError", "rate_limit_exceeded", "Rate Limit hit", "HTTP 429"]
)
def test_a_model_call_that_hit_a_rate_limit_is_a_model_rate_limit(tmp_path, error):
    [fault] = faults_of(tmp_path, {"error": error})

    assert fault.kind is FaultKind.MODEL_RATE_LIMIT
    assert (fault.step, fault.call_id, fault.tool) == (1, None, None)
    assert fault.detail == error


def test_any_other_model_error_is_a_model_error(tmp_path):
    [fault] = faults_of(tmp_path, {"error": "APIConnectionError"})

    assert fault.kind is FaultKind.MODEL_ERROR
    assert fault.detail == "APIConnectionError"


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens"])
def test_a_reply_cut_off_by_the_token_limit_is_a_model_token_limit(tmp_path, reason):
    [fault] = faults_of(tmp_path, {"finish": reason})

    assert fault.kind is FaultKind.MODEL_TOKEN_LIMIT
    assert fault.step == 1
    assert fault.detail == f"finish {reason}"


@pytest.mark.parametrize("reason", ["stop", "tool_calls", "end_turn", "content_filter"])
def test_a_reply_that_ended_for_another_reason_is_not_a_fault(tmp_path, reason):
    assert kinds_of(tmp_path, {"finish": reason}) == []


def test_a_step_fault_comes_before_the_tool_faults_of_the_same_step(tmp_path):
    step = {"finish": "length", "calls": [{"error": "E"}]}

    assert kinds_of(tmp_path, step) == [
        FaultKind.MODEL_TOKEN_LIMIT,
        FaultKind.TOOL_ERROR,
    ]


def test_a_model_fault_is_injected_when_the_step_carries_the_proxys_mark(tmp_path):
    run = run_from(tmp_path, {"error": "RateLimitError"})
    marked = dataclasses.replace(run.steps[0], injected="rate_limit")

    [fault] = detect(dataclasses.replace(run, steps=(marked,)), CONFIG)

    assert fault.origin is Origin.INJECTED


def test_faults_come_in_the_order_they_happened(tmp_path):
    first = calls({"id": "a", "error": "E1"})
    second = calls({"id": "b", "result": json.dumps({"error": "E2"})})

    found = faults_of(tmp_path, first, second)

    assert [(f.step, f.call_id) for f in found] == [(1, "a"), (2, "b")]


def injected(run: Run) -> Run:
    """The same run, with the proxy's mark on its first tool call."""
    step = run.steps[0]
    call = dataclasses.replace(step.tool_calls[0], injected="error")
    marked = dataclasses.replace(step, tool_calls=(call,))
    return dataclasses.replace(run, steps=(marked, *run.steps[1:]))


def test_a_fault_is_injected_when_the_call_carries_the_proxys_mark(tmp_path):
    run = run_from(tmp_path, calls({"id": "a", "error": "E"}))

    [fault] = detect(injected(run), CONFIG)

    assert fault.origin is Origin.INJECTED


def test_find_prints_each_run_with_faults_and_a_summary(tmp_path, capsys):
    path = tmp_path / "spans.jsonl"
    step = calls({"id": "c1", "name": "get_logs", "error": "ConnectionError"})
    write_trace(path, step)
    write_trace(path, calls({"result": "ok"}), trace="clean")

    assert cli.main(["find", str(path)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "run t1",
        "  tool_error step 1 get_logs c1: ConnectionError",
        "faults: tool_error=1",
    ]
