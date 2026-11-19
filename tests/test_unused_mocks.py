"""An unused mock gets one line in the report, of the kind its label allows."""

import io
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from report_cases import AGENT, FOLDER, GOOD, FakeRunner, load
from report_cases import run as a_run
from scrimmage_core import plan
from scrimmage_core.runner import build_report, print_report

NO_TOOLS_LIST = AGENT.replace('tools    = ["acme.tools:get_order", "acme.tools:get_stock"]\n', "")
TEST = """
[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped"]
"""
Fake = Callable[[list[str]], FakeOpenAI]
SEEN = ["get_order", "get_stock"]
TOOL_MOCK = '\n[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped"\n'
ARGS_MOCK = (
    '\n[[test.mock]]\ncall    = "tool:get_order"\nargs    = {order_id = "1"}\nreturns = "shipped"\n'
)
OTHER_ARGS = ARGS_MOCK.replace('"1"', '"2"')
WEB_MOCK = '\n[[test.mock]]\ncall    = "http:inventory.acme.internal"\njson    = {count = 12}\n'
MCP_MOCK = '\n[[test.mock]]\ncall    = "mcp:docs/search"\nreturns = "x"\n'
RECORDED = (
    '\n[[test.mock]]\ncall     = "tool:get_order"\nrecorded = true\n'
    'args     = {order_id = "1"}\nreturns  = "shipped"\n'
)


@dataclass
class Printed:
    code: int
    lines: list[tuple[str, str]]
    text: str

    def about(self, needle: str) -> list[tuple[str, str]]:
        return [(kind, text) for kind, text in self.lines if needle in text]


def printed(agent: str, mocks: str, used: set[int], tools_seen: list[str] | None) -> Printed:
    cfg = load(agent + TEST + mocks)
    (row,) = plan.rows(cfg)
    results = [a_run(row, n, answer=GOOD, mocked=used) for n in range(3)]
    rep = build_report(cfg, results, FOLDER, FakeRunner(tools_seen=tools_seen))  # type: ignore[arg-type]
    out = io.StringIO()
    return Printed(print_report(rep, out), rep.every_message, out.getvalue())


@pytest.mark.design("record.marker_unused_is_note")
def test_an_unused_tool_mock_is_a_note_with_a_tools_list() -> None:
    got = printed(AGENT, TOOL_MOCK, set(), None)
    (found,) = got.about("no run in test 'order-status' called get_order")
    assert found[0] == "notes" and got.code == 0, got.text


@pytest.mark.design("record.marker_unused_is_note")
def test_an_unused_tool_mock_is_a_note_without_a_tools_list() -> None:
    got = printed(NO_TOOLS_LIST, TOOL_MOCK, set(), SEEN)
    (found,) = got.about("no run in test 'order-status' called get_order")
    assert found[0] == "notes" and got.code == 0, got.text
    # no run reported the tools: the labels were never checked, so no note, and a mistake
    unchecked = printed(NO_TOOLS_LIST, TOOL_MOCK, set(), None)
    assert not unchecked.about("called get_order") and unchecked.code == 2


# The six rows of the table in C13b.6: (name, mocks, used, tools list, tools reported, kind,
# text, exit code). The "tool, list" row reports none, so only the tools list checks its label.
ROWS = [
    ("web", WEB_MOCK, set(), True, SEEN, "mistakes", "no request matched the mock on http:", 2),
    (
        "mcp",
        MCP_MOCK,
        set(),
        True,
        SEEN,
        "mistakes",
        "no call matched the mock on mcp:docs/search",
        2,
    ),
    ("tool, no list", TOOL_MOCK, set(), False, SEEN, "notes", "called get_order, so its mock", 0),
    ("tool, list", TOOL_MOCK, set(), True, None, "notes", "called get_order, so its mock", 0),
    (
        "other args",
        ARGS_MOCK + OTHER_ARGS,
        {1},
        True,
        SEEN,
        "notes",
        "called with other arguments",
        0,
    ),
    ("recorded", RECORDED, set(), True, SEEN, "notes", "recorded mock on tool:get_order with", 0),
]


@pytest.mark.design("record.marker_unused_is_note")
@pytest.mark.parametrize(
    ("mocks", "used", "listed", "seen", "kind", "text", "code"),
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_each_kind_of_unused_mock_gets_exactly_one_line(
    mocks: str,
    used: set[int],
    listed: bool,
    seen: list[str] | None,
    kind: str,
    text: str,
    code: int,
) -> None:
    got = printed(AGENT if listed else NO_TOOLS_LIST, mocks, used, seen)
    (found,) = got.about(text)
    assert found[0] == kind, got.text
    assert got.code == code, got.text


@pytest.mark.design("record.marker_unused_is_note")
def test_a_mock_used_only_in_a_fault_row_is_not_unused() -> None:
    fault = (
        '\n[[test.fault]]\ncall   = "tool:get_order"\ndo     = "timeout"\n'
        'expect = [["unable", "sorry"]]\n'
    )
    cfg = load(AGENT + TEST + TOOL_MOCK + fault)
    base, faulted = plan.rows(cfg)
    results = [a_run(base, n, mocked=set()) for n in range(3)]
    results += [a_run(faulted, n, answer="Sorry, unable.", mocked={0}) for n in range(3)]
    rep = build_report(cfg, results, FOLDER, FakeRunner(tools_seen=SEEN))  # type: ignore[arg-type]
    assert not [text for _, text in rep.every_message if "get_order, so its mock" in text]


@pytest.mark.design("record.marker_unused_is_note")
@pytest.mark.slow
def test_an_unused_tool_mock_is_a_note_end_to_end(project: Project, fake_openai: Fake) -> None:
    fake = fake_openai(ACME)
    project.toml.write_text(
        project.toml.read_text()
        + '\n[[test.mock]]\ncall    = "tool:refund_order"\nreturns = "refunded"\n'
    )
    result = project.test(fake)
    assert result.code == 0, result.out + result.err
    assert "no run in test 'order-status' called refund_order, so its mock was not used" in (
        result.out
    )
