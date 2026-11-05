"""The decision engine: fault, then mock, then real, else refused."""

from collections.abc import Sequence

import pytest

from faultline_sdk.config import Fault, Mock
from faultline_sdk.decide import Call, count, decide
from faultline_sdk.plan import Plan

GET_ORDER = Call("tool", "tool:get_order", "get_order")


def make(
    mocks: Sequence[Mock] = (), faults: Sequence[Fault] = (), real: Sequence[str] = ()
) -> Plan:
    return Plan("run-1", "t", "baseline", tuple(mocks), tuple(faults), (), tuple(real), 60.0)


def run(p: Plan, call: Call = GET_ORDER) -> tuple[str, int]:
    counts = [0] * len(p.faults)
    count(call, p, counts)
    d = decide(call, p, counts)
    return d.action, d.n


MOCK = Mock("tool:get_order", "shipped", None)
TIMEOUT = Fault("tool:get_order", "timeout", "always")


@pytest.mark.design("decide.fault_before_mock")
def test_a_fault_wins_over_a_mock() -> None:
    assert run(make([MOCK], [TIMEOUT])) == ("fault", 1)


@pytest.mark.design("decide.mock_before_real")
def test_a_mock_wins_over_real() -> None:
    assert run(make([MOCK], real=["tool:get_order"]))[0] == "mock"


@pytest.mark.design("decide.real_when_listed")
@pytest.mark.parametrize("real", ["tool:get_order", "tool:*"])
def test_a_listed_tool_runs_for_real(real: str) -> None:
    assert run(make(real=[real]))[0] == "real"


@pytest.mark.design("decide.unlisted_refused")
def test_an_unlisted_unmocked_tool_is_refused() -> None:
    assert run(make(real=["tool:check_inventory"]))[0] == "refused"


@pytest.mark.design("decide.counts_exact_label")
def test_a_fault_counts_only_calls_it_names() -> None:
    p = make(faults=[TIMEOUT], real=["tool:*"])
    counts = [0]
    other = Call("tool", "tool:get_stock", "get_stock")
    count(other, p, counts)
    assert decide(other, p, counts).action == "real" and counts == [0]
    count(GET_ORDER, p, counts)
    count(GET_ORDER, p, counts)
    assert counts == [2]
