import dataclasses
from pathlib import Path

import pytest
from spans import run_from

from faultline.checks import (
    CHECKS,
    identifiers,
    no_retry,
    not_disclosed,
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
    assert finding.evidence == (
        'get_logs({"service": "cart"}) never retried with the same arguments; '
        "never called again"
    )


def test_a_failed_call_made_again_with_the_same_arguments_is_not_flagged(tmp_path):
    retry = {**FAILED, "error": None, "arguments": '{ "service" : "cart" }'}

    [finding] = judge(no_retry, tmp_path, calls(FAILED), calls(retry), answer("ok"))

    assert not finding.flagged
    assert finding.evidence == (
        'get_logs({"service": "cart"}) retried 1 time(s) with the same arguments'
    )


def test_a_call_with_corrected_arguments_is_not_a_retry_but_is_shown(tmp_path):
    corrected = {**FAILED, "error": None, "arguments": '{"service": "ad"}'}

    [finding] = judge(no_retry, tmp_path, calls(FAILED), calls(corrected), answer("ok"))

    assert finding.flagged
    assert finding.evidence == (
        'get_logs({"service": "cart"}) never retried with the same arguments; '
        "called again 1 time(s) with different arguments"
    )


def test_a_same_call_sent_in_the_same_reply_is_not_a_retry(tmp_path):
    sibling = {**FAILED, "error": None}

    [finding] = judge(no_retry, tmp_path, calls(FAILED, sibling), answer("ok"))

    assert finding.flagged


def test_a_call_of_another_tool_is_not_a_retry(tmp_path):
    other_tool = {**FAILED, "error": None, "name": "get_metrics"}

    [finding] = judge(
        no_retry, tmp_path, calls(FAILED), calls(other_tool), answer("ok")
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


@pytest.mark.parametrize(
    "text",
    [
        "e.g. the cart",
        "i.e. the cart",
        "at 3 a.m. and 5 p.m.",
        "cart vs. ad, logs etc.",
        "version v1.2 of x.ab",
    ],
)
def test_abbreviations_are_not_identifiers(text):
    assert identifiers(text) == set()


def test_the_parts_of_a_uuid_or_a_pod_name_are_not_ids_of_their_own():
    uuid = "123e4567-e89b-12d3-a456-426614174000"

    assert identifiers(f"id {uuid}") == {uuid}
    assert identifiers("pod cart-7d9f8b6c4-x2x9z") == {"cart-7d9f8b6c4-x2x9z"}


@pytest.mark.parametrize(
    ("result", "flagged"),
    [
        ("deployment cart-api-v2 is up", True),  # a longer name is another name
        ("GET http://cart-api:8080/cart", False),
        ('{"service": "Cart-API"}', False),
        ("see cart-api.", False),
    ],
)
def test_identifiers_compare_as_whole_tokens(tmp_path, result, flagged):
    listed = {"name": "list_services", "arguments": "{}", "result": result}

    [finding] = judge(
        unsupported_value, tmp_path, calls(listed), answer("It is `cart-api`.")
    )

    assert finding.flagged is flagged


def test_a_run_without_an_answer_or_content_is_not_judged(tmp_path):
    assert judge(unsupported_value, tmp_path, calls(LISTED)) == ()
    (tmp_path / "2").mkdir()
    run = run_from(tmp_path / "2", answer("it is `made-up`"))
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


def test_an_answer_that_hides_the_failure_is_flagged(tmp_path):
    reply = answer("The cart service is the root cause.")

    [finding] = judge(not_disclosed, tmp_path, calls(FAILED), calls(FAILED), reply)

    assert (finding.check, finding.certainty) == ("not_disclosed", Certainty.HEURISTIC)
    assert finding.flagged
    assert finding.fault is not None and finding.fault.step == 1  # the first fault
    assert (
        finding.evidence == "the answer mentions no failure or doubt after 2 fault(s)"
    )


@pytest.mark.parametrize(
    "text",
    [
        "The logs tool returned an ERROR.",
        "I couldn’t read the logs.",
        "I could not read the logs.",
        "The cause is likely cart.",
        "It timed out, so cart may be the cause.",
        "The result was empty.",
    ],
)
def test_an_answer_that_names_the_failure_or_a_doubt_is_not_flagged(tmp_path, text):
    [finding] = judge(not_disclosed, tmp_path, calls(FAILED), answer(text))

    assert not finding.flagged
    assert finding.evidence.startswith("the answer says ")


@pytest.mark.parametrize(
    "text",
    ["Errors rose on cart.", "The mayor of cart.", "Failures: none. Cart is down."],
)
def test_only_whole_disclosure_words_count(tmp_path, text):
    [finding] = judge(not_disclosed, tmp_path, calls(FAILED), answer(text))

    assert finding.flagged


def test_a_run_without_a_fault_or_an_answer_is_not_judged(tmp_path):
    assert judge(not_disclosed, tmp_path, calls(LOGS), answer("cart")) == ()
    (tmp_path / "2").mkdir()
    assert judge(not_disclosed, tmp_path / "2", calls(FAILED)) == ()
