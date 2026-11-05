"""The decision engine: fault, then mock, then real, else refused."""

import threading
from collections.abc import Sequence

import pytest

from faultline_sdk.config import Fault, Mock
from faultline_sdk.decide import Call, Counters, decide, fires
from faultline_sdk.labels import matches
from faultline_sdk.plan import Plan

GET_ORDER = Call("tool", "tool:get_order", "get_order")


def make(
    mocks: Sequence[Mock] = (), faults: Sequence[Fault] = (), real: Sequence[str] = ()
) -> Plan:
    return Plan("run-1", "t", "baseline", tuple(mocks), tuple(faults), (), tuple(real), 60.0)


def run(p: Plan, call: Call = GET_ORDER) -> tuple[str, int]:
    d = decide(call, p, Counters(p).note(call))
    return d.action, d.n


def chat(retry: int = 0, key: str | None = "thread-1:abc") -> Call:
    return Call("model", "model:openai", "chat", "openai", "POST", retry, key)


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
    assert run(make(real=["tool:check_inventory"])) == ("refused", 0)


@pytest.mark.design("decide.counts_exact_label")
def test_a_fault_counts_only_calls_it_names() -> None:
    p = make(faults=[TIMEOUT], real=["tool:*"])
    counters = Counters(p)
    other = Call("tool", "tool:get_stock", "get_stock")
    assert decide(other, p, counters.note(other)).action == "real"
    assert counters.attempts == [0]
    counters.note(GET_ORDER)
    counters.note(GET_ORDER)
    assert counters.attempts == [2]


@pytest.mark.design("decide.model_always_real")
def test_a_model_call_runs_for_real_unless_a_fault_fires() -> None:
    assert run(make(), chat()) == ("real", 0)


@pytest.mark.design("decide.model_host_write_refused")
def test_a_write_to_a_model_host_that_is_not_a_model_call_is_refused() -> None:
    tune = Call("http", "http:api.openai.com", "/v1/fine_tuning/jobs", "openai", "POST")
    listing = Call("http", "http:api.openai.com", "/v1/files", "openai", "GET")
    assert decide(tune, make(), []).reason == "model_host_write"
    assert decide(listing, make(), []).action == "real"


@pytest.mark.design("schedule.first_n")
def test_first_n_fails_the_first_n_attempts_retries_included() -> None:
    p = make(faults=[Fault("model", "status 429", "first 3")])
    counters = Counters(p)
    actions = [decide(chat(i), p, counters.note(chat(i))).action for i in range(4)]
    assert actions == ["fault", "fault", "fault", "real"]


@pytest.mark.design("schedule.requests_n")
def test_requests_n_fails_a_request_and_every_retry_of_it() -> None:
    p = make(faults=[Fault("model", "status 429", "requests 1")])
    counters = Counters(p)
    first = [decide(chat(i), p, counters.note(chat(i))).action for i in range(3)]
    second = chat(0, key="thread-1:def")  # a new request: a different body
    assert first == ["fault", "fault", "fault"]
    assert decide(second, p, counters.note(second)).action == "real"


@pytest.mark.design("schedule.requests_parallel")
def test_two_requests_at_once_each_keep_their_own_number() -> None:
    p = make(faults=[Fault("model", "status 429", "requests 1")])
    counters = Counters(p)
    order: list[tuple[str, str]] = []
    start = threading.Barrier(2)

    def send(who: str) -> None:
        start.wait()
        for retry in range(3):
            call = chat(retry, key=f"{who}:same-body")
            order.append((who, decide(call, p, counters.note(call)).action))

    threads = [threading.Thread(target=send, args=(w,)) for w in ("thread-a", "thread-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    by_thread = {w: [a for who, a in order if who == w] for w in ("thread-a", "thread-b")}
    # Exactly one of the two requests is request 1: it and all its retries fail.
    assert sorted(by_thread.values()) == [["fault"] * 3, ["real"] * 3]


def test_a_retry_with_no_first_attempt_counts_as_a_new_request() -> None:
    p = make(faults=[Fault("model", "timeout", "requests 1")])
    counters = Counters(p)
    assert counters.note(chat(2, key="t:x")) == [1]
    assert counters.note(chat(1, key=None)) == [2]


@pytest.mark.design("labels.model_means_chat")
@pytest.mark.parametrize(
    ("label", "call", "expected"),
    [
        ("model", "model:openai", True),
        ("model", "model:openai/embeddings", False),
        ("model:openai", "model:openai", True),
        ("model:*/embeddings", "model:openai/embeddings", True),
        ("model:openai/embeddings", "model:openai/embeddings", True),
        ("model:*/embeddings", "model:openai", False),
        ("tool:get_order", "model:openai", False),
    ],
)
def test_model_labels_match_as_written(label: str, call: str, expected: bool) -> None:
    assert matches(label, call) is expected


def test_schedules() -> None:
    assert fires("always", 1) and fires("first 2", 2) and not fires("first 2", 3)
    assert fires("requests 1", 1) and not fires("requests 1", 2)
    with pytest.raises(ValueError, match="not supported yet"):
        fires("once", 1)
