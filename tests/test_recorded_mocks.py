"""scrimmage test on a file with recorded mocks: exact arguments, and an unused one is a note."""

from collections.abc import Callable

import pytest

from conftest import ACME, EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage_core.config import Mock
from scrimmage_core.runner import recorded_call

Fake = Callable[[list[str]], FakeOpenAI]
HAND_MOCK = '[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n'


def recorded(order_id: str, reply: str = "shipped, Oct 30") -> str:
    return (
        f'[[test.mock]]\ncall     = "tool:get_order"\nrecorded = true\n'
        f'args     = {{order_id = "{order_id}"}}\nreturns  = "{reply}"\n'
    )


def tool_lines(project: Project) -> list[list[tuple[str, str]]]:
    result = Result(0, "", "", project.root)
    return [
        [(str(x["call"]), str(x["action"])) for x in run if x.get("kind") == "tool"]
        for run in result.records()
    ]


@pytest.mark.design("record.marker_unused_is_note")
def test_a_recorded_mock_no_run_used_is_a_note_and_the_test_passes(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(EXAMPLE_FAULT, "")
    project.edit(HAND_MOCK, recorded("1234") + "\n" + recorded("5678", "cancelled"))
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out + result.err
    assert (
        "note: in test 'order-status' the recorded mock on tool:get_order with "
        '{"order_id": "5678"} was not used; this run did not make that call'
    ) in result.out
    assert all(("tool:get_order", "mock") in run for run in tool_lines(project))


@pytest.mark.design("record.marker_unused_is_note")
def test_a_recorded_web_mock_no_run_used_is_a_note_too(project: Project, fake_openai: Fake) -> None:
    project.edit(EXAMPLE_FAULT, "")
    web = (
        '[[test.mock]]\ncall = "http:inventory.acme.internal"\nrecorded = true\n'
        'method = "GET"\npath = "/stock/SKU-42"\njson = {count = 12}\n'
    )
    project.toml.write_text(project.toml.read_text() + "\n" + web)
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out + result.err
    assert (
        "the recorded mock on http:inventory.acme.internal GET /stock/SKU-42 was not used"
    ) in result.out


@pytest.mark.design("config.recorded_setting")
def test_a_hand_written_unused_web_mock_is_still_a_mistake(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(EXAMPLE_FAULT, "")
    web = '[[test.mock]]\ncall = "http:inventory.acme.internal"\njson = {count = 12}\n'
    project.toml.write_text(project.toml.read_text() + "\n" + web)
    result = project.test(fake_openai(ACME))
    assert result.code == 2, result.out + result.err
    assert "no request matched the mock on http:inventory.acme.internal" in result.out + result.err


@pytest.mark.design("decide.recorded_args")
def test_a_call_no_recorded_mock_matches_falls_through_to_the_normal_rule(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(EXAMPLE_FAULT, "")
    project.edit(HAND_MOCK, recorded("5678", "cancelled"))
    project.edit('real     = ["tool:check_inventory"]', 'real     = ["tool:get_order"]')
    result = project.test(fake_openai(ACME))
    # get_order("1234") matched no mock, so it ran for real, as it is under real.
    assert all(("tool:get_order", "real") in run for run in tool_lines(project))
    assert "recorded mock on tool:get_order with" in result.out


def test_a_recorded_mock_is_named_by_its_label_its_arguments_or_its_request() -> None:
    assert recorded_call(Mock("tool:get_order", "x", None, recorded=True)) == "tool:get_order"
    long = Mock("tool:get_order", "x", None, recorded=True, args={"q": "a" * 300})
    assert recorded_call(long).endswith("...") and len(recorded_call(long)) < 240
    assert recorded_call(Mock("http:a.acme", None, None, recorded=True)) == "http:a.acme"
