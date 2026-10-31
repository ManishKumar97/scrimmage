import dataclasses
from pathlib import Path

import pytest
from spans import run_from

from faultline.checks import (
    CHECKS,
    identifiers,
    no_retry,
    repeated_call,
    unsupported_value,
)
from faultline.config import ChecksConfig, DetectConfig
from faultline.detect import detect
from faultline.model import Certainty, Finding

CONFIG = ChecksConfig()


def judge(check, tmp_path: Path, *steps: dict, question: str | None = None):
    run = run_from(tmp_path, *steps, question=question)
    return check(run, detect(run, DetectConfig()), CONFIG)


def calls(*calls: dict) -> dict:
    return {"calls": list(calls)}


def answer(text: str) -> dict:
    return {"text": text}


FAILED = {"name": "get_logs", "arguments": '{"service": "cart"}', "error": "Boom"}


def test_every_check_has_a_unique_name():
    names = [check.name for check in CHECKS]

    assert len(names) == len(set(names))


def test_a_failed_call_never_made_again_is_flagged(tmp_path):
    [finding] = judge(no_retry, tmp_path, calls(FAILED), answer("done"))

    assert isinstance(finding, Finding)
    assert (finding.check, finding.certainty) == ("no_retry", Certainty.CERTAIN)
    assert finding.flagged
    assert finding.fault is not None and finding.fault.tool == "get_logs"
    assert "never called again" in finding.evidence


def test_a_failed_call_made_again_with_the_same_arguments_is_not_flagged(tmp_path):
    retry = {**FAILED, "error": None, "arguments": '{ "service" : "cart" }'}

    [finding] = judge(no_retry, tmp_path, calls(FAILED), calls(retry), answer("ok"))

    assert not finding.flagged
    assert "called again 1 time(s)" in finding.evidence


def test_a_call_with_other_arguments_or_another_tool_is_not_a_retry(tmp_path):
    other_args = {**FAILED, "error": None, "arguments": '{"service": "ad"}'}
    other_tool = {**FAILED, "error": None, "name": "get_metrics"}

    [finding] = judge(
        no_retry, tmp_path, calls(FAILED), calls(other_args, other_tool), answer("ok")
    )

    assert finding.flagged


def test_a_same_call_made_before_the_failure_is_not_a_retry(tmp_path):
    earlier = {**FAILED, "error": None}

    [finding] = judge(no_retry, tmp_path, calls(earlier), calls(FAILED), answer("ok"))

    assert finding.flagged


def test_an_empty_result_is_judged_like_any_tool_fault(tmp_path):
    empty = {"name": "list_pods", "arguments": "{}", "result": "[]"}
    full = {**empty, "result": '["cart-7d9f"]'}

    [finding] = judge(no_retry, tmp_path, calls(empty), calls(full), answer("ok"))

    assert not finding.flagged


def test_a_model_fault_on_the_last_step_is_flagged(tmp_path):
    [finding] = judge(no_retry, tmp_path, calls({}), {"error": "APIError"})

    assert finding.flagged
    assert finding.fault is not None and finding.fault.tool is None
    assert finding.evidence == "model call at step 2 was the last step"


def test_a_model_fault_followed_by_another_step_is_not_flagged(tmp_path):
    [finding] = judge(no_retry, tmp_path, {"error": "APIError"}, answer("ok"))

    assert not finding.flagged


def test_a_run_without_faults_gives_no_findings(tmp_path):
    assert judge(no_retry, tmp_path, calls({}), answer("ok")) == ()


LISTED = {"name": "list_services", "arguments": "{}", "result": "cart, AD-Service"}


def test_an_answer_naming_a_service_no_tool_returned_is_flagged(tmp_path):
    reply = answer("The root cause is `payment-gateway`, not ad-service.")

    [finding] = judge(unsupported_value, tmp_path, calls(LISTED), reply)

    assert finding.flagged
    assert finding.certainty is Certainty.HEURISTIC
    assert finding.fault is None  # the run had no fault
    assert finding.evidence == "answer names payment-gateway, found in no tool result"


def test_an_answer_naming_only_what_the_trace_holds_is_not_flagged(tmp_path):
    reply = answer("`ad-service` failed; see `checkout_v2` and list_services.")

    [finding] = judge(
        unsupported_value,
        tmp_path,
        calls({**LISTED, "arguments": '{"version": "checkout_v2"}'}),
        reply,
    )

    assert not finding.flagged


def test_the_question_supports_a_value(tmp_path):
    reply = answer("It is `cart-api`.")

    [finding] = judge(
        unsupported_value, tmp_path, calls(LISTED), reply, question="Is cart-api slow?"
    )

    assert not finding.flagged


@pytest.mark.parametrize(
    "text",
    [
        "pod cart-7d9f8b6c4-x2x9z restarted",
        "the ad.service host",
        "checkout_v2 timed out",
        "it called 10.0.3.7",
        "trace 4bf92f3577b34da6a3ce929d0e0e4736",
        "id 123e4567-e89b-12d3-a456-426614174000",
        "the `cart-service` pod",
    ],
)
def test_identifiers_of_every_kind_are_found(text):
    assert identifiers(text)


@pytest.mark.parametrize(
    "text",
    [
        "p95 is 8.1s over a 15-minute window, 3.3% errors, version 1.2.3",
        "Plain words like checkout and payment are not identifiers.",
        "gold-tier customers saw a crash-looping pod",
        "It ends here. Next sentence.",
        "deadbeef is a word, 12345678 a number",
        "GetCartAsync and oteldemo.CartService are not lowercase names",
    ],
)
def test_numbers_words_and_english_compounds_are_not_identifiers(text):
    assert identifiers(text) == set()


def test_a_run_without_an_answer_or_content_is_not_judged(tmp_path):
    assert judge(unsupported_value, tmp_path, calls(LISTED)) == ()
    run = run_from(tmp_path, answer("it is `made-up`"))
    stripped = dataclasses.replace(run, content_recorded=False)

    assert unsupported_value(run, (), CONFIG)  # judged
    assert unsupported_value(stripped, (), CONFIG) == ()


def test_a_finding_carries_the_runs_first_fault(tmp_path):
    reply = answer("`made-up` failed")

    [finding] = judge(unsupported_value, tmp_path, calls(FAILED), reply)

    assert finding.fault is not None and finding.fault.tool == "get_logs"


LOGS = {"name": "get_logs", "arguments": '{"service": "cart"}'}


def test_a_call_made_more_than_the_limit_is_flagged_once(tmp_path):
    reordered = {**LOGS, "arguments": '{ "service":"cart" }'}
    steps = [calls(LOGS), calls(LOGS, reordered), calls(LOGS, LOGS), answer("ok")]

    [finding] = judge(repeated_call, tmp_path, *steps)

    assert (finding.check, finding.certainty) == ("repeated_call", Certainty.CERTAIN)
    assert finding.flagged and finding.fault is None
    assert finding.evidence == 'get_logs({"service": "cart"}) called 5 times, limit 3'


def test_a_call_made_exactly_the_limit_is_not_flagged(tmp_path):
    run = run_from(tmp_path, calls(LOGS, LOGS, LOGS), answer("ok"))

    assert repeated_call(run, (), CONFIG) == ()
    [finding] = repeated_call(run, (), ChecksConfig(repeated_call_limit=2))
    assert "called 3 times, limit 2" in finding.evidence


def test_each_pair_over_the_limit_is_its_own_finding(tmp_path):
    other_args = {**LOGS, "arguments": '{"service": "ad"}'}
    other_tool = {**LOGS, "name": "get_metrics"}
    steps = [calls(*[LOGS, other_args, other_tool] * 4), answer("ok")]

    findings = judge(repeated_call, tmp_path, *steps)

    assert len(findings) == 3


def test_a_run_without_recorded_content_is_not_judged(tmp_path):
    run = run_from(tmp_path, calls(*[LOGS] * 5), answer("ok"))
    stripped = dataclasses.replace(run, content_recorded=False)

    assert repeated_call(run, (), CONFIG)
    assert repeated_call(stripped, (), CONFIG) == ()
