import dataclasses
from typing import Any

import pytest

from faultline.model import (
    Certainty,
    Fault,
    FaultKind,
    Finding,
    Origin,
    Run,
    RunVerdict,
    Step,
    ToolCall,
    Verdict,
)


def a_call(**changes: Any) -> ToolCall:
    call = ToolCall(
        name="get_logs", call_id="a", arguments="{}", seconds=0.5, result="ok"
    )
    return dataclasses.replace(call, **changes)


def a_step(number: int = 1, **changes: Any) -> Step:
    step = Step(
        number=number,
        input_tokens=100,
        output_tokens=20,
        finish_reason="stop",
        text="done",
    )
    return dataclasses.replace(step, **changes)


def a_run(**changes: Any) -> Run:
    run = Run(
        trace_id="t",
        model="m",
        stop_reason="answered",
        question="why?",
        answer="because",
        seconds=1.0,
        complete=True,
    )
    return dataclasses.replace(run, **changes)


def a_fault() -> Fault:
    return Fault(
        kind=FaultKind.TOOL_ERROR,
        trace_id="t",
        step=1,
        call_id="a",
        tool="get_logs",
        detail="timeout",
        origin=Origin.OBSERVED,
    )


EVERY_TYPE = [
    (a_call(), "name", "other"),
    (a_step(), "text", "other"),
    (a_run(), "answer", "other"),
    (a_fault(), "detail", "other"),
    (
        Finding(
            fault=a_fault(),
            check="no_retry",
            certainty=Certainty.CERTAIN,
            flagged=True,
            evidence="called once",
        ),
        "flagged",
        False,
    ),
    (
        RunVerdict(trace_id="t", verdict=Verdict.PASSED, reason="ok", fault=None),
        "verdict",
        Verdict.FAILED_SILENTLY,
    ),
]


@pytest.mark.parametrize(("value", "name", "new"), EVERY_TYPE)
def test_every_type_is_frozen(value: object, name: str, new: object):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, name, new)


@pytest.mark.parametrize("value", [value for value, _, _ in EVERY_TYPE])
def test_every_type_has_slots_so_no_new_attribute_can_be_added(value: object):
    assert not hasattr(value, "__dict__")


def test_collections_are_tuples_by_default():
    run = a_run()

    assert run.steps == ()
    assert run.unplaced == ()
    assert a_step().tool_calls == ()


def test_collections_hold_tuples_that_cannot_grow():
    step = a_step(tool_calls=(a_call(),))
    run = a_run(steps=(step,), unplaced=(a_call(call_id="b"),))

    assert isinstance(run.steps, tuple)
    assert isinstance(run.steps[0].tool_calls, tuple)
    assert isinstance(run.tool_calls, tuple)
    assert [call.call_id for call in run.tool_calls] == ["a", "b"]


def test_a_run_has_content_and_no_injected_fault_by_default():
    assert a_run().content_recorded is True
    assert a_step().injected is None
    assert a_call().injected is None


def test_enum_values_are_the_names_written_to_reports():
    assert [kind.value for kind in FaultKind] == [
        "tool_error",
        "tool_timeout",
        "empty_result",
        "schema_mismatch",
        "model_error",
        "model_rate_limit",
        "model_token_limit",
    ]
    assert [origin.value for origin in Origin] == ["observed", "injected"]
    assert [c.value for c in Certainty] == ["certain", "heuristic"]
    assert [verdict.value for verdict in Verdict] == [
        "passed",
        "failed_loudly",
        "failed_silently",
    ]
    assert FaultKind.TOOL_ERROR == "tool_error"  # a StrEnum compares as its value


def test_run_tokens_add_input_and_output_of_every_step():
    steps = (
        a_step(1, input_tokens=100, output_tokens=20),
        a_step(2, input_tokens=None, output_tokens=5),
        a_step(3, input_tokens=7, output_tokens=None),
    )

    assert a_run(steps=steps).tokens == 132
    assert a_run().tokens == 0
