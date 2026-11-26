"""scrimmage test on a file with recorded mocks: exact arguments, and an unused one is a note."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import config_cases
from conftest import ACME, EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from report_cases import AGENT, TEST, load
from report_cases import run as a_run
from scrimmage import config as settings
from scrimmage import plan, state, tool_gate
from scrimmage.config import Mock
from scrimmage.errors import ScrimmageRefused
from scrimmage.plan import Plan
from scrimmage.runner import recorded_call, unused_mocks

# Every test here, or nearly, starts a process or a real scrimmage test run.
pytestmark = pytest.mark.slow

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


# A mock with args on a tool, written by hand: exact, and a miss is visible.


def refund_order(order_id: str, marker: Path) -> str:
    marker.write_text(f"refunded {order_id}")
    return "refunded for real"


def get_stock(sku: str) -> str:
    return f"{sku}: 12 in stock"


def tools_run(lines: "Lines", mocks: list[Mock], real: list[str]) -> state.RunState:
    p = Plan("run-1", "t", "baseline", tuple(mocks), (), (), tuple(real), 60.0)
    run = state.RunState(p, lines, ["tests.", "test_recorded_mocks."])
    state.install(run)
    return run


class Lines:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append(line)


@pytest.fixture
def lines() -> Iterator[Lines]:
    yield Lines()
    state.install(None)


def refund_mock(tmp_path: Path) -> Mock:
    """The mock as a team writes it in scrimmage.toml, read by the real parser."""
    text = config_cases.mock(
        'call = "tool:refund_order"\nreturns = "refunded 1234"\nargs = {order_id = "1234"}\n'
    )
    path = tmp_path / "scrimmage.toml"
    path.write_text(text)
    return settings.load(path).tests[0].mocks[0]


@pytest.mark.design("decide.hand_written_args")
def test_a_refund_of_another_order_is_refused_and_never_runs(tmp_path: Path, lines: Lines) -> None:
    marker = tmp_path / "refunded.txt"
    tools_run(lines, [refund_mock(tmp_path)], [])
    refund = tool_gate.gate(lambda order_id: refund_order(order_id, marker), "refund_order")
    assert refund("1234") == "refunded 1234"  # the mock answers the order it names
    with pytest.raises(ScrimmageRefused, match="none matches this call's arguments"):
        refund("5678")
    assert not marker.exists()
    assert [line["action"] for line in lines.lines] == ["mock", "refused"]


@pytest.mark.design("decide.hand_written_args")
def test_a_miss_under_a_wildcard_is_refused_and_never_runs(tmp_path: Path, lines: Lines) -> None:
    marker = tmp_path / "refunded.txt"
    tools_run(lines, [refund_mock(tmp_path)], ["tool:*"])
    refund = tool_gate.gate(lambda order_id: refund_order(order_id, marker), "refund_order")
    with pytest.raises(ScrimmageRefused):
        refund("5678")
    assert not marker.exists() and lines.lines[0]["action"] == "refused"
    # listed by name, the same call runs for real: the team said so
    tools_run(Lines(), [refund_mock(tmp_path)], ["tool:refund_order"])
    assert refund("5678") == "refunded for real" and marker.exists()


@pytest.mark.design("decide.hand_written_args")
def test_a_wildcard_still_runs_a_tool_with_no_args_mock(tmp_path: Path, lines: Lines) -> None:
    tools_run(lines, [refund_mock(tmp_path)], ["tool:*"])
    assert tool_gate.gate(get_stock, "get_stock")("SKU-42") == "SKU-42: 12 in stock"
    assert lines.lines[0]["action"] == "real" and "mock_miss" not in lines.lines[0]


def with_orders(project: Project, mock: str, real: str) -> None:
    project.edit(EXAMPLE_FAULT, "")
    project.edit(HAND_MOCK, mock)
    project.edit('real     = ["tool:check_inventory"]', f'real     = ["{real}"]')


@pytest.mark.design("record.marker_unused_is_note")
def test_a_recorded_mock_a_call_missed_says_so(project: Project, fake_openai: Fake) -> None:
    with_orders(project, recorded("5678", "cancelled"), "tool:get_order")
    result = project.test(fake_openai(ACME))
    assert (
        'the recorded mock on tool:get_order with {"order_id": "5678"} was not used; '
        "a call with other arguments missed it; see the warning"
    ) in result.out
    assert 'warning: tool:get_order with {"order_id": "1234"} matched no mock' in result.out


@pytest.mark.design("record.marker_unused_is_note")
def test_an_unused_hand_written_mock_on_a_called_label_is_a_note(
    project: Project, fake_openai: Fake
) -> None:
    hand = HAND_MOCK.replace("returns", 'args    = {order_id = "5678"}\nreturns')
    with_orders(project, hand, "tool:get_order")
    result = project.test(fake_openai(ACME))
    assert result.code != 2, result.out
    assert (
        'note: in test \'order-status\' the mock on tool:get_order with {"order_id": "5678"} '
        "was not used; the tool was called with other arguments"
    ) in result.out
    assert "no mistake" not in result.out and "test mistake" not in result.out


@pytest.mark.design("record.marker_unused_is_note")
def test_an_unused_hand_written_mock_on_an_uncalled_mcp_label_is_a_mistake() -> None:
    mock = '\n[[test.mock]]\ncall    = "mcp:docs/search"\nargs    = {q = "a"}\nreturns = "x"\n'
    cfg = load(AGENT + TEST + mock)
    row = plan.rows(cfg)[0]
    notes, mistakes, _ = unused_mocks(cfg, [a_run(row, 0, mocked=set())])
    assert any("no call matched the mock on mcp:docs/search" in m for m in mistakes)
    assert not any("mcp:docs/search" in n for n in notes)


@pytest.mark.design("record.marker_unused_is_note")
def test_a_test_whose_runs_all_hit_an_infrastructure_error_reports_no_unused_mock() -> None:
    mock = '\n[[test.mock]]\ncall    = "mcp:docs/search"\nargs    = {q = "a"}\nreturns = "x"\n'
    cfg = load(AGENT + TEST + mock)
    row = plan.rows(cfg)[0]
    runs = [a_run(row, 0, "infrastructure error", None, mocked=set())]
    assert unused_mocks(cfg, runs) == ([], [], [])
