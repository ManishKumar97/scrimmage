"""The order bot in LangGraph: the team writes no tools list, the adapter finds the tools."""

import threading
import time
import types
from collections.abc import Callable
from typing import Any

import pytest

from conftest import ACME, Project, Result
from fakes.openai_server import FakeOpenAI
from faultline_sdk import langchain_gate, state, tool_gate
from faultline_sdk.config import Fault
from faultline_sdk.errors import FaultlineRefused
from faultline_sdk.plan import Plan
from faultline_sdk.start import BadAnswerPath, pick

Fake = Callable[[list[str]], FakeOpenAI]
TOOLS_LINE = (
    'tools    = ["acme.tools:get_order", "acme.tools:check_inventory", '
    '"acme.tools:refund_order", "acme.tools:get_stock"]\n'
)
TIMEOUT = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'


def graph(project: Project, function: str = "answer", fault: str = TIMEOUT, runs: int = 1) -> None:
    """The S1 file without its tools line, on the LangGraph bot."""
    project.edit(TOOLS_LINE, "")
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "acme.graph:{function}"\n'
        f'answer   = "messages[-1].content"\nruns     = {runs}\nparallel = 1',
    )
    project.edit(TIMEOUT, fault)


def error_fault(extra: str = "") -> str:
    return f'[[test.fault]]\ncall = "tool:get_order"\ndo   = "error"\n{extra}'


def row(result: Result, name: str) -> list[dict[str, object]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)


def tool_lines(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    return [x for x in lines if x.get("kind") == "tool"]


def told(fake: FakeOpenAI) -> set[str]:
    """What LangChain told the model about tools, from the requests it sent."""
    return {
        str(m["content"])
        for request in fake.requests
        for m in request["messages"]
        if m.get("role") == "tool"
    }


@pytest.mark.design("adapter.no_tools_list")
def test_the_same_file_without_a_tools_list_runs_the_same_rows(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, runs=3)
    result = project.test(fake_openai(ACME))
    assert len(result.records()) == 6
    base = row(result, "baseline")
    assert [(x["call"], x["action"]) for x in tool_lines(base)] == [
        ("tool:get_order", "mock"),
        ("tool:get_stock", "mock"),
    ]
    assert base[-1]["summary"]["answer"] == (  # type: ignore[index]
        "Order 1234 shipped on Oct 30. SKU-42 has 12 in stock."
    )
    fault = row(result, "fault 1")
    assert fault[-1]["summary"]["fault_fired"] is True  # type: ignore[index]


@pytest.mark.design("adapter.typed_mock_from_tool")
def test_a_mock_becomes_the_type_the_tool_function_returns(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault="")
    fake = fake_openai(ACME)
    project.test(fake)
    assert any("Stock" in text or "count" in text for text in told(fake)), told(fake)


@pytest.mark.design("adapter.crashed_outcome")
def test_an_error_fault_with_no_handler_ends_the_graph_as_crashed(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault=error_fault())
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 0, result.out + result.err
    fault = row(result, "fault 1")
    assert fault[-1]["summary"]["outcome"] == "crashed"  # type: ignore[index]
    assert str(tool_lines(fault)[0]["error"]).startswith("ToolException: get_order failed")
    assert not fake.errors


@pytest.mark.design("adapter.error_default_type")
def test_an_error_fault_reaches_langchains_handler_as_toolexception(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, "answer_handled", error_fault())
    fake = fake_openai(ACME)
    result = project.test(fake)
    fault = row(result, "fault 1")
    assert fault[-1]["summary"]["outcome"] == "answered"  # type: ignore[index]
    assert "get_order failed" in told(fake)


@pytest.mark.design("fault.error_raises")
def test_a_fault_can_name_another_error_type(project: Project) -> None:
    graph(project, fault=error_fault('raises = "ConnectionError"\n'))
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    line = tool_lines(row(result, "fault 1"))[0]
    assert str(line["error"]).startswith("ConnectionError: get_order failed")
    assert row(result, "fault 1")[-1]["summary"]["outcome"] == "crashed"  # type: ignore[index]


@pytest.mark.design("adapter.parallel_call_numbers")
def test_three_parallel_tool_calls_get_three_distinct_numbers(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, "answer_handled", error_fault())
    result = project.test(fake_openai(["acme_three_orders", "acme_answer"]))
    lines = tool_lines(row(result, "fault 1"))
    numbers = sorted(x["fault"]["n"] for x in lines)  # type: ignore[index]
    assert numbers == [1, 2, 3], result.out


@pytest.mark.design("run.counters_shared_safely")
def test_calls_at_the_same_moment_never_share_a_number() -> None:
    class Lines:
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            del line, keep

    fault = Fault("tool:t", "error", "always")
    plan = Plan("run-1", "t", "fault 1", (), (fault,), (), (), 60.0)
    run = state.RunState(plan, Lines(), [])

    class YieldingCounts(list[int]):
        """Gives up the thread between reading a count and storing it, as a race would."""

        def __setitem__(self, index: Any, value: Any) -> None:
            time.sleep(0.0005)
            super().__setitem__(index, value)

    run.counters.attempts = YieldingCounts(run.counters.attempts)
    barrier = threading.Barrier(8)
    numbers: list[int] = []

    def call() -> None:
        barrier.wait()
        numbers.extend(run.begin("t", {}).decision.n for _ in range(5))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(numbers) == list(range(1, 8 * 5 + 1))


@pytest.mark.design("proof.real_against_injected_tool_error")
@pytest.mark.parametrize(
    ("real_function", "injected_function", "outcome"),
    [
        ("real_failure", "answer", "crashed"),
        ("real_failure_handled", "answer_handled", "answered"),
    ],
)
def test_a_real_toolexception_and_an_injected_error_are_handled_alike(
    project: Project, real_function: str, injected_function: str, outcome: str
) -> None:
    graph(project, injected_function, error_fault())
    injected_fake = FakeOpenAI(ACME)
    injected_fake.start()
    try:
        injected = project.test(injected_fake)
    finally:
        injected_fake.stop()
    injected_run = row(injected, "fault 1")

    # The same bot, with a get_order whose own code raises ToolException.
    project.edit(
        f'function = "acme.graph:{injected_function}"', f'function = "acme.graph:{real_function}"'
    )
    project.edit(error_fault(), "")
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:get_order"]')
    real_fake = FakeOpenAI(ACME)
    real_fake.start()
    try:
        real = project.test(real_fake)
    finally:
        real_fake.stop()
    real_run = [
        lines for lines in real.records() if lines[-1].get("run") != injected_run[-1].get("run")
    ][-1]

    def seen(lines: list[dict[str, object]]) -> tuple[object, object]:
        line = tool_lines(lines)[0]
        return str(line["error"]), lines[-1]["summary"]["outcome"]  # type: ignore[index]

    assert seen(real_run)[0] == seen(injected_run)[0] == "ToolException: get_order failed"
    assert seen(real_run)[1] == seen(injected_run)[1] == outcome
    # The same failure message reached the model, or none did.
    assert {t for t in told(real_fake) if "failed" in t} == {
        t for t in told(injected_fake) if "failed" in t
    }
    assert tool_lines(real_run)[0]["action"] == "real"
    assert tool_lines(injected_run)[0]["action"] == "fault"


@pytest.mark.design("adapter.listed_framework_tool")
def test_a_framework_tool_can_also_be_listed_under_tools(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault="")
    project.edit("[agent]\n", '[agent]\ntools = ["acme.graph:get_order", "acme.graph:get_stock"]\n')
    project.edit('real     = ["tool:check_inventory"]', "real = []")
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out + result.err
    assert [x["action"] for x in tool_lines(result.records()[0])] == ["mock", "mock"]


@pytest.mark.design("adapter.class_tool")
def test_a_tool_written_as_a_class_is_gated(project: Project, fake_openai: Fake) -> None:
    graph(project, "answer_custom_class")
    result = project.test(fake_openai(ACME))
    assert [x["action"] for x in tool_lines(row(result, "baseline"))] == ["mock", "mock"]


@pytest.mark.design("adapter.mixin_tool")
def test_a_run_function_from_a_mixin_is_gated(project: Project) -> None:
    graph(project, "mixin_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    result = project.test()
    assert result.code == 2, result.out + result.err
    assert {s["outcome"] for s in result.summaries()} == {"refused call"}
    assert "REAL WRITE RAN" not in (result.summaries()[0]["answer"] or "")  # type: ignore[operator]


@pytest.mark.design("adapter.invoke_override_stops")
def test_a_tool_that_overrides_invoke_stops_the_test_if_offered(project: Project) -> None:
    graph(project, "answer_overridden", "")
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2 and fake.requests == []
    assert "overrides_invoke" in result.out


@pytest.mark.design("adapter.async_graph")
def test_an_async_graph_is_gated_too(project: Project, fake_openai: Fake) -> None:
    graph(project, "answer_async")
    result = project.test(fake_openai(ACME))
    assert [x["action"] for x in tool_lines(row(result, "baseline"))] == ["mock", "mock"]


@pytest.mark.design("gate.same_kind_async_tool")
def test_an_async_tool_gets_an_async_gate(project: Project) -> None:
    project.edit('returns = "shipped, Oct 30"', 'returns = "mocked"')
    project.edit('call    = "tool:get_order"', 'call    = "tool:get_order_async"')
    graph(project, "async_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    result = project.test()
    (lines,) = result.records()
    assert lines[-1]["summary"]["answer"] == "mocked"  # type: ignore[index]


@pytest.mark.design("adapter.artifact_mock")
@pytest.mark.parametrize(
    ("returns", "answer", "code"),
    [('["the report", {a = "b"}]', "the report", 0), ('"just text"', None, 2)],
)
def test_a_tool_with_an_artifact_is_mocked_with_a_list_of_two(
    project: Project, returns: str, answer: str | None, code: int
) -> None:
    project.edit('returns = "shipped, Oct 30"', f"returns = {returns}")
    project.edit('call    = "tool:get_order"', 'call    = "tool:get_report"')
    graph(project, "report_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    result = project.test()
    assert result.code == code, result.out + result.err
    assert result.summaries()[0]["answer"] == answer


@pytest.mark.design("adapter.one_call_one_line")
def test_arun_delegating_to_run_is_one_call(project: Project) -> None:
    graph(project, "sync_tool_through_arun", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    (lines,) = project.test().records()
    assert [x["call"] for x in tool_lines(lines)] == ["tool:check_inventory"]
    assert lines[-1]["summary"]["answer"] == "SKU-42: 12 in stock"  # type: ignore[index]


@pytest.mark.design("adapter.nested_tool_gated")
def test_a_tool_called_from_inside_a_tool_is_a_call_of_its_own(project: Project) -> None:
    graph(project, "nested", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:*"]')
    (lines,) = project.test().records()
    # The inner call ends first, so it is written first.
    assert [x["call"] for x in tool_lines(lines)] == ["tool:check_inventory", "tool:outer"]


@pytest.mark.design("adapter.write_refused")
def test_an_unlisted_write_tool_is_refused_and_never_runs(project: Project) -> None:
    graph(project, fault="")
    fake = FakeOpenAI(["acme_refund_order", "acme_answer"])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2
    assert {s["outcome"] for s in result.summaries()} == {"refused call"}
    assert "must never run" not in result.out


@pytest.mark.design("adapter.tools_count_as_seen")
def test_a_frameworks_tools_count_as_seen_but_a_tool_no_gate_reaches_stops_the_test(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, "answer_unseen", "")
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2 and fake.requests == []
    assert "delete_all" in result.out and "get_order" not in result.out.split("delete_all")[0][-20:]


def module(**names: object) -> types.ModuleType:
    shim = types.ModuleType("shim")
    vars(shim).update(names)
    return shim


class Shape:
    """What the adapter expects of BaseTool."""

    def __init__(self, **kwargs: Any) -> None:
        self.name = str(kwargs.get("name", ""))

    def _run(self, *args: Any, **kwargs: Any) -> str:
        return "real"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return "real"

    def run(self, tool_input: object) -> str:
        return self._run(tool_input)

    async def arun(self, tool_input: object) -> str:
        return await self._arun(tool_input)

    def invoke(self, tool_input: object) -> str:
        return self.run(tool_input)

    async def ainvoke(self, tool_input: object) -> str:
        return await self.arun(tool_input)


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        del keep
        self.lines.append(line)


def fresh_run() -> tuple[state.RunState, Sink]:
    sink = Sink()
    return state.RunState(Plan("r", "t", "baseline", (), (), (), (), 60.0), sink, []), sink


@pytest.mark.design("adapter.structural_check")
@pytest.mark.parametrize(
    "shim",
    [
        module(),
        module(BaseTool=Shape),
        module(BaseTool=type("Empty", (), {}), ToolException=Exception),
        module(
            BaseTool=type("BaseTool", (), {**dict(vars(Shape)), "_run": lambda self: "x"}),
            ToolException=Exception,
        ),
        module(
            BaseTool=type("BaseTool", (), {**dict(vars(Shape)), "run": lambda self, x: "x"}),
            ToolException=Exception,
        ),
    ],
)
def test_a_library_the_adapter_cannot_replace_stops_the_program(shim: types.ModuleType) -> None:
    run, sink = fresh_run()
    stopped: list[str] = []
    langchain_gate.patch(shim, run, stopped.append)
    assert stopped and "the program is stopped" in stopped[0]
    assert sink.lines[0]["notice"] == "structure"


def base_tool() -> type:
    return type("BaseTool", (), dict(vars(Shape)))


def test_a_newer_library_gets_a_notice_and_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langchain_gate, "version", lambda: (99, 0))
    run, sink = fresh_run()
    base = base_tool()
    langchain_gate.patch(module(BaseTool=base, ToolException=Exception), run, pytest.fail)
    assert sink.lines[0]["notice"] == "newer_version"

    class Later(base):  # type: ignore[valid-type, misc]
        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "real"

    Later(name="later")
    assert getattr(vars(Later)["_run"], tool_gate.GATED)
    assert "later" in run.seen


@pytest.mark.design("adapter.tools_made_before_the_hook")
def test_a_tool_made_before_the_hook_is_gated_when_it_is_called() -> None:
    run, sink = fresh_run()
    state.install(run)
    base = base_tool()

    class Early(base):  # type: ignore[valid-type, misc]
        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "real"

    early = Early(name="early")
    try:
        langchain_gate.patch(module(BaseTool=base, ToolException=Exception), run, pytest.fail)
        with pytest.raises(FaultlineRefused):
            early.run({})  # listed nowhere, so refused, not run
    finally:
        state.install(None)
    assert sink.lines[0]["action"] == "refused"


@pytest.mark.design("adapter.late_assignment")
def test_a_run_function_assigned_later_is_still_gated() -> None:
    run, sink = fresh_run()
    state.install(run)
    base = base_tool()
    try:
        langchain_gate.patch(module(BaseTool=base, ToolException=Exception), run, pytest.fail)

        class Plain(base):  # type: ignore[valid-type, misc]
            pass

        tool = Plain(name="plain")
        Plain._run = lambda self, *a, **k: "REAL"  # type: ignore[method-assign]
        with pytest.raises(FaultlineRefused):
            tool.run({})
    finally:
        state.install(None)
    assert sink.lines[0]["action"] == "refused"


@pytest.mark.design("adapter.not_a_function_refused")
def test_a_run_that_is_not_a_plain_function_is_refused_and_never_seen() -> None:
    run, sink = fresh_run()
    state.install(run)
    base = base_tool()
    try:
        langchain_gate.patch(module(BaseTool=base, ToolException=Exception), run, pytest.fail)

        class Static(base):  # type: ignore[valid-type, misc]
            _run = staticmethod(lambda *a, **k: "REAL")  # type: ignore[assignment]

        tool = Static(name="static")
        assert "static" not in run.seen
        with pytest.raises(FaultlineRefused, match="cannot be gated"):
            tool.run({})
    finally:
        state.install(None)
    assert "cannot be gated" in str(sink.lines[0]["mistake"])


@pytest.mark.parametrize(
    ("value", "path", "expected"),
    [
        ({"reply": "hi"}, "reply", "hi"),
        ({"messages": ["a", "b"]}, "messages[-1]", "b"),
        (types.SimpleNamespace(text="hi"), "text", "hi"),
        ({"messages": [types.SimpleNamespace(content="c")]}, "messages[-1].content", "c"),
    ],
)
def test_the_answer_path_reads_keys_indexes_and_attributes(
    value: object, path: str, expected: str
) -> None:
    assert pick(value, path) == expected


@pytest.mark.design("start.answer_path")
@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("reply", "does not fit what the agent returned"),
        ("messages[5]", "does not fit what the agent returned"),
        ("messages[0].nope", "does not fit what the agent returned"),
        ("messages[0].upper", "which is not an answer"),
        ("empty", "which is not an answer"),
    ],
)
def test_an_answer_path_that_does_not_fit_says_so(path: str, message: str) -> None:
    with pytest.raises(BadAnswerPath, match=message):
        pick({"messages": ["a"], "empty": None}, path)


@pytest.mark.design("start.answer_path_is_a_mistake")
def test_a_wrong_answer_path_is_a_test_mistake_not_a_result(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault="")
    project.edit('answer   = "messages[-1].content"', 'answer   = "messages[-1].nope"')
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out + result.err
    assert "does not fit what the agent returned" in result.out
