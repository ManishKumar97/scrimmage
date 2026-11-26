"""The decision engine: fault, then mock, then real, else refused."""

import dataclasses
import threading
from collections.abc import Sequence

import pytest

from scrimmage.config import Fault, Mock
from scrimmage.decide import (
    Call,
    Counters,
    Decision,
    Miss,
    alike,
    decide,
    fires,
    labelled,
    missed,
    names,
    on_label,
    schedule,
)
from scrimmage.labels import matches
from scrimmage.plan import Plan
from scrimmage.state import RunState

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


@pytest.mark.design("decide.web_write_refused")
def test_a_write_to_a_model_host_that_is_not_a_model_call_is_refused() -> None:
    tune = Call("http", "http:api.openai.com", "/v1/fine_tuning/jobs", "openai", "POST")
    listing = Call("http", "http:api.openai.com", "/v1/files", "openai", "GET")
    assert decide(tune, make(), []).reason == "web_write"
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


@pytest.mark.design("schedule.call_n")
def test_call_n_fires_on_the_nth_matching_call_alone() -> None:
    p = make(faults=[Fault("tool:get_order", "timeout", "call 2")], real=["tool:*"])
    counters = Counters(p)
    other = Call("tool", "tool:get_stock", "get_stock")
    actions = []
    for call in (GET_ORDER, other, GET_ORDER, GET_ORDER, GET_ORDER):
        decision = decide(call, p, counters.note(call))
        actions.append((decision.action, decision.n))
    # The call it does not name takes no number; the second get_order alone fails.
    assert actions == [("real", 0), ("real", 0), ("fault", 2), ("real", 0), ("real", 0)]


@pytest.mark.design("schedule.call_n_attempts")
def test_call_n_counts_a_retry_as_the_next_call() -> None:
    p = make(faults=[Fault("model", "status 429", "call 2")])
    counters = Counters(p)
    actions = [decide(chat(i), p, counters.note(chat(i))).action for i in range(3)]
    # The library's first retry is the second call, as under first N.
    assert actions == ["real", "fault", "real"]


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
    assert fires("call 2", 2) and not fires("call 2", 1) and not fires("call 2", 3)
    with pytest.raises(ValueError, match="is not a schedule"):
        fires("once", 1)


def named(model: str | None, *, label: str = "model:openai") -> Call:
    return Call("model", label, "chat", "openai", "POST", model=model)


MAIN_FAULT = Fault("model", "status 429", "first 3", model="gpt-4.1")


@pytest.mark.design("decide.model_name_exact")
@pytest.mark.parametrize(
    ("sent", "fires"),
    [
        ("gpt-4.1", True),
        ("gpt-4.1-mini", False),  # the very trap of a prefix
        ("gpt-4.1-2025-04-14", False),
        ("GPT-4.1", False),
        ("gpt-4", False),
        ("", False),
    ],
)
def test_a_fault_names_the_whole_model_name_case_and_all(sent: str, fires: bool) -> None:
    assert names(MAIN_FAULT, named(sent)) is fires


@pytest.mark.design("decide.dated_name_is_another_name")
def test_a_plain_name_does_not_name_its_dated_snapshot() -> None:
    fault = Fault("model", "status 429", "always", model="gpt-4o-mini")
    assert not names(fault, named("gpt-4o-mini-2024-07-18"))


@pytest.mark.design("decide.model_name_counts")
def test_a_model_fault_counts_only_calls_to_that_model() -> None:
    p = make(faults=[MAIN_FAULT])
    counters = Counters(p)
    router, main = named("gpt-4o-mini"), named("gpt-4.1")
    assert decide(router, p, counters.note(router)).action == "real"
    assert counters.attempts == [0]
    numbers = [decide(main, p, counters.note(main)).n for _ in range(3)]
    assert numbers == [1, 2, 3] and decide(router, p, counters.note(router)).action == "real"


@pytest.mark.design("decide.model_name_decides")
def test_the_decision_itself_compares_the_name() -> None:
    p = make(faults=[MAIN_FAULT])
    assert decide(named("gpt-4o-mini"), p, [1]).action == "real"
    assert decide(named("gpt-4.1"), p, [1]).action == "fault"


@pytest.mark.design("decide.model_name_requests")
def test_requests_n_keeps_a_retry_with_its_request_past_another_model() -> None:
    p = make(faults=[Fault("model", "status 429", "requests 1", model="gpt-4.1")])
    counters = Counters(p)
    main = dataclasses.replace(named("gpt-4.1"), key="thread-1:main")
    router = dataclasses.replace(named("gpt-4o-mini"), key="thread-1:router")
    assert decide(main, p, counters.note(main)).action == "fault"
    assert decide(router, p, counters.note(router)).action == "real"
    retry = dataclasses.replace(main, retry=1)
    assert decide(retry, p, counters.note(retry)).n == 1  # still the first request


@pytest.mark.design("decide.no_name_fails_closed")
def test_a_call_with_no_readable_name_is_refused_when_a_fault_names_a_model() -> None:
    p = make(faults=[MAIN_FAULT])
    call = named(None)
    counters = Counters(p)
    decision = decide(call, p, counters.note(call))
    assert (decision.action, decision.reason) == ("refused", "unreadable_model")
    assert counters.attempts == [0]  # a call that may not be the target takes no number


@pytest.mark.design("decide.no_name_after_the_schedule")
def test_the_refusal_does_not_depend_on_what_the_schedule_has_left() -> None:
    p = make(faults=[Fault("model", "status 429", "first 1", model="gpt-4.1")])
    counters = Counters(p)
    main = named("gpt-4.1")
    counters.note(main)
    counters.note(main)
    assert decide(named(None), p, counters.note(named(None))).reason == "unreadable_model"


@pytest.mark.design("decide.no_name_only_under_its_label")
def test_a_call_the_faults_label_does_not_name_is_left_alone() -> None:
    p = make(faults=[MAIN_FAULT])
    embedding = named(None, label="model:openai/embeddings")
    assert decide(embedding, p, Counters(p).note(embedding)).action == "real"


@pytest.mark.design("decide.no_name_no_change")
def test_a_fault_with_no_model_needs_no_name() -> None:
    p = make(faults=[Fault("model", "status 429", "always")])
    call = named(None)
    assert decide(call, p, Counters(p).note(call)).action == "fault"


@pytest.mark.design("decide.no_name_without_a_named_fault")
def test_a_plan_with_no_fault_needs_no_name() -> None:
    assert decide(named(None), make(), []).action == "real"


@pytest.mark.design("decide.fault_before_the_refusal")
def test_a_fault_that_fires_comes_before_the_refusal_as_the_decision_order_says() -> None:
    p = make(faults=[Fault("model", "status 429", "always"), MAIN_FAULT])
    assert decide(named(None), p, [1, 0]).action == "fault"


RECORDED = Mock("tool:get_order", "order 1", None, recorded=True, args={"order_id": "1"})


def get_order_call(args: object) -> Call:
    return dataclasses.replace(GET_ORDER, args=args)


@pytest.mark.design("decide.recorded_args")
def test_a_recorded_mock_answers_only_its_exact_arguments() -> None:
    p = make([RECORDED], real=["tool:get_order"])
    assert run(p, get_order_call({"order_id": "1"}))[0] == "mock"
    assert run(p, get_order_call({"order_id": "2"}))[0] == "real"
    assert run(p, get_order_call({"order_id": 1}))[0] == "real"  # type-strict: 1 is not "1"
    assert run(p, get_order_call(None))[0] == "real"  # arguments no mock can hold
    assert run(make([RECORDED]), get_order_call({"order_id": "2"})) == ("refused", 0)


@pytest.mark.design("decide.recorded_args")
def test_a_call_one_recorded_mock_misses_goes_on_to_the_next_one() -> None:
    second = dataclasses.replace(RECORDED, returns="order 2", args={"order_id": "2"})
    found = decide(get_order_call({"order_id": "2"}), make([RECORDED, second]), [])
    assert (found.action, found.mock) == ("mock", 1)


@pytest.mark.design("decide.recorded_args")
@pytest.mark.parametrize("args", [{"order_id": "1"}, {"order_id": "2"}, {"order_id": 1}, None])
def test_a_hand_written_mock_answers_every_call(args: object) -> None:
    assert run(make([MOCK]), get_order_call(args))[0] == "mock"


HAND = Mock("tool:get_order", "order 1", None, args={"order_id": "1"})


@pytest.mark.design("decide.hand_written_args")
def test_a_hand_written_mock_with_args_answers_only_those_arguments() -> None:
    p = make([HAND], real=["tool:get_order"])
    assert run(p, get_order_call({"order_id": "1"}))[0] == "mock"
    assert run(p, get_order_call({"order_id": "2"}))[0] == "real"
    assert run(p, get_order_call({"order_id": 1}))[0] == "real"  # type-strict
    assert run(p, get_order_call({"order_id": "1", "extra": 1}))[0] == "real"  # no subset match
    assert run(p, get_order_call(object()))[0] == "real"  # arguments that are not known
    assert run(p, get_order_call(None))[0] == "real"
    counted = Mock("tool:get_order", "x", None, args={"n": 1})
    for other in (True, 1.0):  # type-strict at every level: True is not 1, 1.0 is not 1
        found = run(make([counted], real=["tool:get_order"]), get_order_call({"n": other}))
        assert found[0] == "real"
    assert run(make([HAND]), get_order_call({"order_id": "2"})) == ("refused", 0)


@pytest.mark.design("decide.miss_evidence")
def test_a_miss_under_a_wildcard_is_refused() -> None:
    p = make([HAND], real=["tool:*"])
    other = get_order_call({"order_id": "2"})
    assert decide(other, p, []) == Decision("refused", reason="unlisted")
    # by name it runs for real; so does a call the mock answers
    named_p = make([HAND], real=["tool:get_order"])
    assert decide(other, named_p, []).action == "real"
    assert decide(get_order_call({"order_id": "1"}), p, []).action == "mock"


@pytest.mark.design("decide.miss_evidence")
def test_missed_names_the_nearest_mock_by_fewest_differing_keys() -> None:
    far = Mock("tool:get_order", "a", None, args={"order_id": "9", "wh": "x", "n": 1})
    near = Mock("tool:get_order", "b", None, args={"order_id": "9", "wh": "x"})
    tie = Mock("tool:get_order", "c", None, args={"order_id": "9", "wh": "x"})
    p = make([far, near, tie], real=["tool:get_order"])
    call = get_order_call({"order_id": "1", "wh": "x"})
    found = missed(call, p, decide(call, p, []))
    assert found is not None
    assert (found.reason, found.nearest, found.differs) == ("arguments", 1, ("order_id",))
    # an added key and a type change count as differences
    added = get_order_call({"order_id": "9", "wh": "x", "extra": True})
    other = missed(added, p, decide(added, p, []))
    assert other is not None and other.differs == ("extra",)
    retyped = get_order_call({"order_id": 9, "wh": "x"})
    third = missed(retyped, p, decide(retyped, p, []))
    assert third is not None and third.differs == ("order_id",)
    # arguments that are not known: the first mock, nothing said to differ
    unknown = get_order_call(None)
    fourth = missed(unknown, p, decide(unknown, p, []))
    assert fourth is not None and (fourth.nearest, fourth.differs, fourth.known) == (0, (), False)


@pytest.mark.design("decide.miss_evidence")
def test_a_call_answered_by_a_fault_or_a_mock_is_not_a_miss() -> None:
    p = make([HAND], [TIMEOUT], real=["tool:get_order"])
    call = get_order_call({"order_id": "2"})
    assert missed(call, p, decide(call, p, [1])) is None  # a fault answered
    q = make([HAND], real=["tool:get_order"])
    same = get_order_call({"order_id": "1"})
    assert missed(same, q, decide(same, q, [])) is None  # a mock answered
    assert missed(call, q, Decision("refused", reason="poisoned")) is None
    model = Call("model", "model:openai", "chat")
    assert missed(model, q, Decision("real")) is None
    process = Call("process", "process:python", "python")
    assert missed(process, q, Decision("refused", reason="unlisted")) is None


@pytest.mark.design("decide.miss_evidence")
def test_a_listed_tool_with_no_mock_is_not_a_miss() -> None:
    p = make([HAND], real=["tool:get_order", "tool:get_stock"])
    stock = Call("tool", "tool:get_stock", "get_stock", args={"sku": "1"})
    assert decide(stock, p, []).action == "real"
    assert missed(stock, p, decide(stock, p, [])) is None


@pytest.mark.design("decide.miss_evidence")
def test_a_refused_call_with_no_mock_is_a_no_mock_miss() -> None:
    p = make([HAND], real=["tool:get_order"])
    stock = Call("tool", "tool:get_stock", "get_stock", args={"sku": "1"})
    decision = decide(stock, p, [])
    found = missed(stock, p, decision)
    assert decision.action == "refused"
    assert found is not None and (found.reason, found.nearest) == ("no_mock", None)
    # with no mock that pins arguments in the plan, a refusal is the old refusal
    plain = make([MOCK])
    assert missed(stock, plain, decide(stock, plain, [])) is None


APPEND = Fault("tool:get_order", "append", "always", text="x")


def test_a_carried_fault_over_a_real_call_runs_it() -> None:
    p = make(faults=[APPEND], real=["tool:get_order"])
    decision = decide(GET_ORDER, p, Counters(p).note(GET_ORDER))
    assert (decision.action, decision.under, decision.fault, decision.n) == ("fault", "real", 0, 1)
    assert decision.mock is None
    slow = make(faults=[Fault("tool:get_order", "slow 2s", "always")], real=["tool:*"])
    assert decide(GET_ORDER, slow, [1]).under == "real"


def test_a_carried_fault_over_a_mock_keeps_the_mock() -> None:
    p = make([MOCK], [APPEND])
    decision = decide(GET_ORDER, p, [1])
    assert (decision.action, decision.under, decision.mock) == ("fault", "mock", 0)
    # A fault that is not carried still replaces the call, with no mock beneath it.
    other = decide(GET_ORDER, make([MOCK], [TIMEOUT]), [1])
    assert (other.under, other.mock) == (None, None)


def test_a_carried_fault_over_a_refused_call_is_refused_as_in_the_baseline() -> None:
    p = make(faults=[APPEND], real=["tool:check_inventory"])
    baseline = decide(GET_ORDER, make(real=["tool:check_inventory"]), [])
    decision = decide(GET_ORDER, p, [1])
    assert decision == baseline == Decision("refused", reason="unlisted")
    # A mock with args that this call misses, and a name not listed, is the same refusal.
    args = make([HAND], [APPEND])
    assert decide(get_order_call({"order_id": "2"}), args, [1]).action == "refused"


def test_a_carried_fault_never_lets_an_unlisted_web_write_run() -> None:
    post = Call("http", "http:orders.acme.internal", "/refunds", None, "POST")
    fault = Fault("http:orders.acme.internal", "append", "always", text="x")
    p = make(faults=[fault])
    assert decide(post, p, [1]) == Decision("refused", reason="web_write")
    get = Call("http", "http:orders.acme.internal", "/orders/1", None, "GET")
    assert decide(get, p, [1]).under == "real"
    listed = make(faults=[fault], real=["http:orders.acme.internal POST"])
    assert decide(post, listed, [1]).under == "real"


def test_a_carried_fault_counts_its_calls_like_any_fault() -> None:
    first = Fault("tool:get_order", "append", "first 2", text="x")
    p = make(faults=[first], real=["tool:get_order"])
    counters = Counters(p)
    seen = [decide(GET_ORDER, p, counters.note(GET_ORDER)) for _ in range(3)]
    assert [d.action for d in seen] == ["fault", "fault", "real"]
    assert [d.n for d in seen] == [1, 2, 0]
    # A refused call uses up its place too.
    refused = make(faults=[first])
    counters = Counters(refused)
    decide(GET_ORDER, refused, counters.note(GET_ORDER))
    assert counters.attempts == [1]


def test_a_miss_under_a_carried_fault_keeps_its_evidence() -> None:
    p = make([HAND], [APPEND], real=["tool:get_order"])
    call = get_order_call({"order_id": "2"})
    decision = decide(call, p, [1])
    assert (decision.action, decision.under) == ("fault", "real")
    assert missed(call, p, decision) is None  # the engine's own decision is a fault
    found = RunState.found_miss(call, p, decision)
    base = missed(call, make([HAND], real=["tool:get_order"]), Decision("real"))
    assert found is not None and found == base
    assert found.reason == "arguments" and found.differs == ("order_id",)
    # A call a mock answers under a carried fault is not a miss.
    same = get_order_call({"order_id": "1"})
    assert RunState.found_miss(same, p, decide(same, p, [1])) is None


def fresh(text: str) -> str:
    """An equal string that is not the same object, as one read from a file or a request is."""
    return "".join(list(text))


WEB_LABEL = "http:orders.acme.internal"


def web(name: str = "/orders", method: str = "GET") -> Call:
    return Call("http", WEB_LABEL, name, None, method)


@pytest.mark.design("decide.web_narrowing")
def test_a_web_mock_narrowed_to_a_method_names_only_that_method() -> None:
    mock = Mock(WEB_LABEL, None, None, method="GET")
    for sent, named_by_it in [("GET", True), ("POST", False), ("DELETE", False), ("PUT", False)]:
        assert labelled(mock, web(method=sent)) is named_by_it
    assert labelled(mock, web(method=fresh("GET")))  # equal is enough, identical is not needed
    assert labelled(Mock(WEB_LABEL, None, None, method=fresh("GET")), web(method="GET"))
    assert labelled(Mock(WEB_LABEL, None, None), web(method="POST"))  # no method: any method


@pytest.mark.design("decide.web_narrowing")
def test_a_web_mock_narrowed_to_a_path_names_only_that_path() -> None:
    mock = Mock(WEB_LABEL, None, None, path="/b")
    for sent, named_by_it in [("/a", False), ("/b", True), ("/c", False), ("/b/c", False)]:
        assert labelled(mock, web(sent)) is named_by_it
    assert labelled(mock, web(fresh("/b")))
    assert labelled(Mock(WEB_LABEL, None, None, path=fresh("/b")), web("/b"))
    assert labelled(Mock(WEB_LABEL, None, None), web("/anything"))


@pytest.mark.design("decide.web_narrowing")
def test_a_fault_narrowed_to_a_method_and_a_path_is_counted_only_for_that_request() -> None:
    fault = Fault(WEB_LABEL, "status 503", "always", method="POST", path="/orders")
    p = make(faults=[fault], real=[WEB_LABEL])
    counters = Counters(p)
    for call in (web(method="GET"), web("/other", "POST"), web(method="PUT")):
        assert counters.note(call) == [0]
    assert counters.note(web(method="POST")) == [1]


@pytest.mark.design("decide.mcp_short_label")
def test_an_mcp_mock_by_its_short_name_is_on_the_tools_label_and_names_it() -> None:
    call = Call("mcp", "mcp:docs/search", "search", short=fresh("search"))
    mock = Mock("search", None, None)
    assert labelled(mock, call) and on_label(mock, call)
    assert not on_label(Mock("lookup", None, None), call)


@pytest.mark.design("decide.schedule")
def test_a_schedule_reads_to_its_kind_and_count_and_rejects_what_is_not_one() -> None:
    assert schedule("always") == ("always", 0)
    assert schedule("first 3") == ("first", 3)
    assert schedule("requests 2") == ("requests", 2)
    assert schedule("call 10") == ("call", 10)
    for bad in ("", "Always", "bogus 3", "first", "first x", "first -1", "call"):
        with pytest.raises(ValueError, match="is not a schedule"):
            schedule(bad)


@pytest.mark.design("decide.schedule")
def test_a_schedule_fires_only_on_the_numbers_of_a_call_that_was_counted() -> None:
    assert not fires("always", 0) and fires("always", 5)
    assert not fires("first 3", 0) and fires("first 3", 1) and not fires("first 3", 4)
    assert not fires("requests 3", 0) and fires("requests 3", 3)
    assert fires("call 1000", 1000) and not fires("call 1000", 999) and not fires("call 1000", 1001)


@pytest.mark.design("decide.counts_exact_label")
def test_every_fault_that_names_a_call_is_counted_and_the_rest_keep_zero() -> None:
    other = Fault("tool:other", "timeout", "always")
    first = Fault("tool:get_order", "timeout", "first 5")
    p = make(faults=[other, TIMEOUT, first])
    counters = Counters(p)
    assert counters.note(GET_ORDER) == [0, 1, 1]
    assert counters.note(GET_ORDER) == [0, 2, 2]
    assert counters.attempts == [0, 2, 2]


@pytest.mark.design("decide.counts_exact_label")
def test_only_a_retry_of_a_request_the_counters_know_keeps_that_requests_number() -> None:
    p = make(faults=[Fault("model", "status 429", "requests 2")])
    counters = Counters(p)
    assert counters.note(chat(0, "a")) == [1]
    assert counters.note(chat(0, "a")) == [2]  # a first attempt is a new request, key or no
    assert counters.note(chat(1, "a")) == [2]  # a retry of a request the counters know
    assert counters.note(chat(1, "b")) == [3]  # a retry of one they never saw
    assert counters.note(chat(1, None)) == [4]  # with no key, nothing ties it to a request


def test_alike_ranks_the_path_that_shares_more_leading_segments_higher() -> None:
    assert alike("/a/b", "/a/c") > alike("/x/b", "/a/c")
    assert alike("/a/b", "/a/c") > alike("/x/b", "/a/b")  # a differing segment ends the count
    assert alike("/a/b", "/a/b") > alike("/a/b", "/a/c")
    assert alike("/a", "/a/b") > alike("/x", "/a/b")  # a shorter path ends it; no error
    assert alike("/a/b", "/a") > alike("/x/b", "/a")
    assert alike("/a", "/a") > alike(None, "/a")  # no path on the mock shares nothing


@pytest.mark.design("decide.miss_evidence")
def test_a_missed_web_request_names_the_nearest_mock_of_its_own_label() -> None:
    before = Mock("http:billing.acme.internal", None, None, path="/orders/1/status")
    after = Mock("http:zeta.acme.internal", None, None, path="/orders/1/status")
    far = Mock(WEB_LABEL, None, None, path="/orders/9")
    near = Mock(WEB_LABEL, None, None, path="/orders/1/refund")
    p = make([before, after, far, near], real=[WEB_LABEL])
    for label in (WEB_LABEL, fresh(WEB_LABEL)):  # the same label, read from the request
        call = Call("http", label, "/orders/1/status", None, "GET")
        decision = decide(call, p, [])
        assert decision.action == "real"
        assert missed(call, p, decision) == Miss("request", 3, ("path",), True)


@pytest.mark.design("decide.miss_evidence")
def test_a_missed_web_request_prefers_the_mock_of_its_own_method_to_a_nearer_path() -> None:
    nearer_path = Mock(WEB_LABEL, None, None, method="POST", path="/orders/1/status")
    own_method = Mock(WEB_LABEL, None, None, method="GET", path="/orders/2")
    p = make([nearer_path, own_method], real=[WEB_LABEL])
    call = web("/orders/1/status", "GET")
    assert missed(call, p, decide(call, p, [])) == Miss("request", 1, ("path",), True)
    assert missed(web("/x", "GET"), make(real=[WEB_LABEL]), Decision("real")) is None


@pytest.mark.design("decide.miss_evidence")
def test_a_missed_call_says_whether_its_arguments_could_be_read() -> None:
    p = make([HAND], real=["tool:get_order"])
    call = get_order_call({"order_id": "2"})
    assert missed(call, p, decide(call, p, [])) == Miss("arguments", 0, ("order_id",), True)
    assert missed(get_order_call(None), p, Decision("real")) == Miss("arguments", 0, (), False)


@pytest.mark.design("decide.web_write_refused")
def test_a_real_entry_for_one_method_and_path_lets_only_that_web_write_run() -> None:
    p = make(real=[f"{WEB_LABEL} POST /orders"])
    assert decide(web("/orders", "POST"), p, []) == Decision("real")
    assert decide(web("/refunds", "POST"), p, []) == Decision("refused", reason="web_write")
    assert decide(web("/orders", "DELETE"), p, []) == Decision("refused", reason="web_write")
    assert decide(web("/refunds", "GET"), p, []) == Decision("real")  # a read needs no entry
    host = make(real=[WEB_LABEL])
    assert decide(web("/anything", "POST"), host, []) == Decision("real")


def test_a_call_and_a_decision_are_made_once_and_never_changed() -> None:
    for made, name in (
        (GET_ORDER, "label"),
        (Decision("real"), "reason"),
        (Miss("no_mock"), "known"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(made, name, "x")


def test_a_call_is_a_marked_first_attempt_unless_said_otherwise() -> None:
    call = Call("tool", "tool:x", "x")
    assert (call.retry, call.marked, call.args, call.key) == (0, True, None, None)
