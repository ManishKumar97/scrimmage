from dataclasses import replace
from decimal import Decimal

import pytest

from faultline.faults import parse
from faultline.model import Run, RunVerdict, Step, ToolCall, Verdict
from faultline.verdict import (
    NO_REFERENCE,
    STREAMED,
    Reference,
    judge,
    line,
    never_applied,
    numbers,
)

EMPTY_LOGS = ToolCall("get_logs", "", "{}", 0.1, "", injected="tool:get_logs:empty")
LOGS = ToolCall("get_logs", "", "{}", 0.1, "cart-7d9f: OOMKilled")
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
# A clean run's answer, the reference when the team gave no expected answer.
CLEAN = Reference(
    "The `cart-api` pod restarted 3 times. ROOT CAUSE SERVICE: cart-api",
    expected=False,
    source="clean run 1",
)
JSON_CLEAN = Reference('{"service": "cart-api", "restarts": 3}', False, "clean run 1")


def ran(
    answer: str | None,
    faulted: bool = True,
    finish: str | None = "stop",
    step_error: str | None = None,
    error: str | None = None,
) -> Run:
    """A two-step run: a get_logs call, then a last reply with this answer."""
    call = EMPTY_LOGS if faulted else LOGS
    last = Step(2, 10, 2, finish, answer or "", step_error)
    return replace(
        RUN,
        answer=answer,
        error_type=error,
        steps=(Step(1, 10, 2, "tool_call", "", tool_calls=(call,)), last),
    )


def same_service(answer: str, reference: str) -> bool:
    """A team's matcher: only the ROOT CAUSE SERVICE line counts."""
    return answer.rsplit(":", 1)[-1].strip() == reference.rsplit(":", 1)[-1].strip()


# (case, run, exit code, reference, the verdict). One per branch of the rules.
CASES = [
    ("crashed", ran("cart-api"), 1, CLEAN, Verdict.FAILED_LOUDLY),
    ("run ended in error", ran("x", error="boom"), 0, CLEAN, Verdict.FAILED_LOUDLY),
    (
        "last model call failed",
        ran(None, finish=None, step_error="500"),
        0,
        CLEAN,
        Verdict.FAILED_LOUDLY,
    ),
    (
        "token limit",
        ran("The `cart-api` pod rest", finish="length"),
        0,
        CLEAN,
        Verdict.FAILED_LOUDLY,
    ),
    (
        "step limit: still asking for a tool",
        ran(None, finish="tool_call"),
        0,
        CLEAN,
        Verdict.FAILED_LOUDLY,
    ),
    ("no answer", ran(None), 0, CLEAN, Verdict.FAILED_LOUDLY),
    (
        "same identifiers and numbers, other words",
        ran("Pod `cart-api` had 3 restarts. ROOT CAUSE SERVICE: cart-api"),
        0,
        CLEAN,
        Verdict.PASSED,
    ),
    (
        "another service, nothing disclosed",
        ran("The `payment-api` pod restarted 3 times. ROOT CAUSE SERVICE: payment-api"),
        0,
        CLEAN,
        Verdict.FAILED_SILENTLY,
    ),
    (
        "another number, nothing disclosed",
        ran("The `cart-api` pod restarted 5 times. ROOT CAUSE SERVICE: cart-api"),
        0,
        CLEAN,
        Verdict.FAILED_SILENTLY,
    ),
    (
        "different, but in doubt",
        ran("It might be `payment-api`. ROOT CAUSE SERVICE: payment-api"),
        0,
        CLEAN,
        Verdict.PASSED,
    ),
    (
        "said it could not complete",
        ran("I could not read the logs, so I cannot name a cause."),
        0,
        CLEAN,
        Verdict.PASSED,
    ),
    (
        "same JSON, other key order",
        ran('{"restarts": 3, "service": "cart-api"}'),
        0,
        JSON_CLEAN,
        Verdict.PASSED,
    ),
    (
        "different JSON",
        ran('{"service": "cart-api", "restarts": 0}'),
        0,
        JSON_CLEAN,
        Verdict.FAILED_SILENTLY,
    ),
    (
        "clean run against the team's expected answer",
        ran("ROOT CAUSE SERVICE: payment", faulted=False),
        0,
        Reference("cart", True, "the expected answer"),
        Verdict.FAILED_SILENTLY,
    ),
    (
        "the expected service, in other words",
        ran("The cart is down. ROOT CAUSE SERVICE: cart"),
        0,
        Reference("cart", True, "the expected answer"),
        Verdict.PASSED,
    ),
]


@pytest.mark.parametrize(
    ("run", "code", "reference", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_each_rule_gives_its_verdict(run, code, reference, expected):
    verdict = judge(run, "t", code, reference)

    assert isinstance(verdict, RunVerdict), verdict
    assert verdict.verdict == expected, verdict.reason


def test_the_table_covers_at_least_ten_cases_and_every_verdict():
    assert len(CASES) >= 10
    assert {case[-1] for case in CASES} == set(Verdict)


def test_reasons_say_what_differs_and_what_was_injected():
    wrong = ran("The `payment-api` pod restarted 3 times.")
    doubtful = ran("It might be `payment-api`.")

    assert line(judge(wrong, "t", 0, CLEAN)) == (
        'verdict: failed_silently (answer lacks "cart-api", compared with clean run 1; '
        "injected tool:get_logs:empty)"
    )
    assert line(judge(doubtful, "t", 0, CLEAN)) == (
        'verdict: passed (answer lacks "3", "cart-api", compared with clean run 1, '
        'but says "might"; injected tool:get_logs:empty)'
    )
    assert line(judge(ran(None, finish="tool_call"), "t", 0, CLEAN)) == (
        "verdict: failed_loudly (the run ended while the model asked for a tool: "
        "a step, token or time limit; injected tool:get_logs:empty)"
    )


def test_a_clean_run_that_differs_from_another_clean_run_is_not_called_wrong():
    differs = ran("ROOT CAUSE SERVICE: `payment-api`", faulted=False)

    verdict = judge(differs, "t", 0, CLEAN)

    assert verdict == (
        'no verdict: answer lacks "3", "cart-api", compared with clean run 1, with no '
        "fault injected and no expected answer, so neither answer is known right"
    )


def test_a_team_matcher_replaces_the_comparison():
    other_words = ran("Totally different text. ROOT CAUSE SERVICE: cart-api")
    other_service = ran("ROOT CAUSE SERVICE: payment-api")

    assert line(judge(other_words, "t", 0, CLEAN, matcher=same_service)) == (
        "verdict: passed (answer matches clean run 1; injected tool:get_logs:empty)"
    )
    assert line(judge(other_service, "t", 0, CLEAN, matcher=same_service)) == (
        "verdict: failed_silently (answer differs by the team's matcher, compared "
        "with clean run 1; injected tool:get_logs:empty)"
    )


def test_a_matcher_that_raises_gives_no_verdict():
    def broken(answer: str, reference: str) -> bool:
        raise KeyError("service")

    assert judge(ran("x"), "t", 0, CLEAN, matcher=broken) == (
        "no verdict: the matcher raised KeyError: 'service'"
    )


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("3 restarts, 8.1s, 50% and 1,000 errors.", {"3", "8.1", "50", "1000"}),
        ("It is 5.", {"5"}),
        ("pod cart-7d9f, v1.2.3, 10.0.0.1, 2026-10-31 12:30, 0x1f, abc123", set()),
    ],
)
def test_numbers_are_whole_tokens_never_part_of_a_name_address_or_date(text, found):
    assert set(numbers(text)) == {Decimal(n) for n in found}


def test_an_answer_without_the_expected_text_under_a_fault_failed_silently():
    verdict = judge(RUN, "t", 0, "cart")

    assert isinstance(verdict, RunVerdict)
    assert verdict.verdict == Verdict.FAILED_SILENTLY
    assert verdict.fault == "tool:get_logs:empty"
    assert line(verdict) == (
        'verdict: failed_silently (answer lacks "cart", compared with the expected '
        "answer; injected tool:get_logs:empty)"
    )


def test_an_answer_with_the_expected_text_passed():
    verdict = judge(replace(RUN, answer="ROOT CAUSE SERVICE: cart"), "t", 0, "cart")

    assert line(verdict) == (
        "verdict: passed (answer matches the expected answer; "
        "injected tool:get_logs:empty)"
    )


def test_a_clean_run_has_no_fault_in_its_verdict():
    clean = replace(RUN, steps=(Step(1, 10, 2, "stop", "cart"),), answer="cart")

    verdict = judge(clean, "t", 0, "cart")

    assert isinstance(verdict, RunVerdict)
    assert verdict.fault is None
    assert line(verdict) == "verdict: passed (answer matches the expected answer)"


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
        "verdict: failed_loudly (the last model call failed: 429; "
        "injected model:rate_limit)"
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
