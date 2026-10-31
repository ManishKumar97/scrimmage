from dataclasses import replace

from faultline.faults import parse
from faultline.model import Run, RunVerdict, Step, ToolCall, Verdict
from faultline.verdict import NO_REFERENCE, STREAMED, judge, line, never_applied

EMPTY_LOGS = ToolCall("get_logs", "", "{}", 0.1, "", injected="tool:get_logs:empty")
RUN = Run(
    trace_id="t",
    model="m",
    stop_reason=None,
    question="why?",
    answer="No fault found. ROOT CAUSE SERVICE: none",
    seconds=1.0,
    complete=True,
    steps=(
        Step(1, 10, 2, "tool_call", "", tool_calls=(EMPTY_LOGS,)),
        Step(2, 10, 2, "stop", "No fault found."),
    ),
)


def test_an_answer_without_the_expected_text_under_a_fault_failed_silently():
    verdict = judge(RUN, "t", 0, "cart")

    assert isinstance(verdict, RunVerdict)
    assert verdict.verdict == Verdict.FAILED_SILENTLY
    assert verdict.fault == "tool:get_logs:empty"
    assert line(verdict) == (
        'verdict: failed_silently (answer lacks "cart"; injected tool:get_logs:empty)'
    )


def test_an_answer_with_the_expected_text_passed():
    verdict = judge(replace(RUN, answer="ROOT CAUSE SERVICE: cart"), "t", 0, "cart")

    assert line(verdict) == (
        'verdict: passed (answer has "cart"; injected tool:get_logs:empty)'
    )


def test_a_clean_run_has_no_fault_in_its_verdict():
    clean = replace(RUN, steps=(Step(1, 10, 2, "stop", "cart"),), answer="cart")

    verdict = judge(clean, "t", 0, "cart")

    assert isinstance(verdict, RunVerdict)
    assert verdict.fault is None
    assert line(verdict) == 'verdict: passed (answer has "cart")'


def test_rule_one_comes_first_even_without_a_reference():
    rate_limited = replace(
        RUN,
        answer=None,
        steps=(Step(1, None, None, None, "", "429", injected="model:rate_limit"),),
    )

    assert line(judge(RUN, "t", 3, None)) == (
        "verdict: failed_loudly (exit code 3; injected tool:get_logs:empty)"
    )
    assert line(judge(None, "t", 0, "cart")) == (
        "verdict: failed_loudly (no model call recorded)"
    )
    assert line(judge(rate_limited, "t", 0, None)) == (
        "verdict: failed_loudly (no answer; injected model:rate_limit)"
    )
    assert line(judge(replace(RUN, error_type="exit 1"), "t", 0, "x")).startswith(
        "verdict: failed_loudly (error exit 1"
    )


def test_without_a_reference_there_is_no_verdict_past_rule_one():
    assert judge(RUN, "t", 0, None) == NO_REFERENCE
    assert line(NO_REFERENCE) == NO_REFERENCE


def test_an_unread_streamed_answer_is_no_verdict_not_a_failure():
    streamed = replace(RUN, answer=None)

    assert judge(streamed, "t", 0, "cart", answer_unrecorded=True) == STREAMED
    assert line(STREAMED) == "no verdict: answer not recorded (streamed reply)"
    # A crash is still a crash, stream or not.
    assert line(judge(streamed, "t", 1, "cart", answer_unrecorded=True)).startswith(
        "verdict: failed_loudly (exit code 1"
    )


def test_a_tool_fault_on_a_call_the_tool_never_reached_says_how_many_it_got():
    missed = never_applied([parse("tool:get_logs:error@3")], RUN, wrapped=True)

    assert missed == [
        (
            "tool:get_logs:error@3",
            "the agent called get_logs 1 time(s), so there was no call 3",
        )
    ]
