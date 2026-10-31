from pathlib import Path

from spans import run_from

from faultline.checks import CHECKS, no_retry
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
