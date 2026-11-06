"""The order bot in LangGraph: the team writes no tools list, the adapter finds the tools."""

import functools
import threading
import time
import types
from collections.abc import Callable
from typing import Any

import pytest

from conftest import ACME, Project, Result
from fakes.openai_server import FakeOpenAI
from faultline_sdk import config, langchain_gate, plan, state, tool_gate
from faultline_sdk.config import Fault
from faultline_sdk.errors import FaultlineRefused
from faultline_sdk.plan import Plan
from faultline_sdk.runner import Runner, RunResult
from faultline_sdk.start import BadAnswerPath, pick

Fake = Callable[[list[str]], FakeOpenAI]
TOOLS_LINE = (
    'tools    = ["acme.tools:get_order", "acme.tools:check_inventory", '
    '"acme.tools:refund_order", "acme.tools:get_stock"]\n'
)
STOCK_MOCK = (
    '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock object\n'
    'returns = {sku = "SKU-42", count = 12}\n'
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
    graph(project)
    result = project.test(fake_openai(ACME))
    assert len(result.records()) == 2
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
    assert any("Stock(" in text for text in told(fake)), told(fake)


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


@pytest.mark.design("adapter.handler_forms")
@pytest.mark.parametrize(
    ("function", "text"),
    [
        ("answer_handled_text", "the orders system is down"),
        ("answer_handled_callable", "handled: get_order failed"),
        ("answer_handled_async", "get_order failed"),
    ],
)
def test_every_form_of_handle_tool_error_gets_the_injected_error(
    project: Project, fake_openai: Fake, function: str, text: str
) -> None:
    graph(project, function, error_fault())
    fake = fake_openai(ACME)
    result = project.test(fake)
    assert row(result, "fault 1")[-1]["summary"]["outcome"] == "answered"  # type: ignore[index]
    assert text in told(fake)


@pytest.mark.design("adapter.label_is_model_name")
def test_a_label_uses_the_name_the_model_sees(project: Project) -> None:
    project.edit('call    = "tool:get_order"', 'call    = "tool:lookup_order"')
    graph(project, "renamed", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    result = project.test()
    assert result.summaries()[0]["answer"] == "shipped, Oct 30"


@pytest.mark.design("adapter.plain_tool_class")
def test_the_plain_tool_class_is_gated(project: Project) -> None:
    graph(project, "plain_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    result = project.test()
    assert result.summaries()[0]["answer"] == "shipped, Oct 30"


@pytest.mark.design("adapter.structure_stops_end_to_end")
def test_a_changed_library_stops_the_run_with_exit_3(project: Project) -> None:
    graph(project, "nested", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("acme.graph:nested", "acme.probe:answer")
    (project.root / "acme" / "probe.py").write_text(
        "import langchain_core.tools.base\n\n\ndef answer(question):\n    return 'ran'\n"
    )
    tools = project.root / "langchain_core" / "tools"
    tools.mkdir(parents=True)
    (project.root / "langchain_core" / "__init__.py").write_text("")
    (tools / "__init__.py").write_text("")
    (tools / "base.py").write_text("class BaseTool:\n    pass\n")
    result = project.test()
    assert result.code == 3, result.out + result.err
    assert "does not have the functions Faultline gates" in result.out


@pytest.mark.design("labels.fault_typo_costs_one_run")
def test_a_misspelled_fault_stops_the_test_after_one_baseline_run(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault=TIMEOUT.replace("tool:get_order", "tool:get_ordr"), runs=3)
    project.edit("parallel = 1", "parallel = 4")
    fake = fake_openai(ACME)
    result = project.test(fake)
    assert result.code == 2, result.out + result.err
    assert len(result.records()) == 1  # one baseline run, not six
    assert "tool:get_ordr names no tool the agent has" in result.out
    assert "the tools are check_inventory, get_order, " in result.out


@pytest.mark.design("labels.mock_typo_on_real_tool")
def test_a_misspelled_mock_of_a_tool_under_real_stops_the_test(
    project: Project, fake_openai: Fake
) -> None:
    graph(project, fault="")
    project.edit('call    = "tool:get_stock"', 'call    = "tool:check_inventry"')
    project.edit('returns = {sku = "SKU-42", count = 12}', 'returns = "12 in stock"')
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:*"]')
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out + result.err
    assert len(result.records()) == 1
    assert "tool:check_inventry names no tool the agent has" in result.out


@pytest.mark.design("labels.never_used_mock")
def test_a_mock_no_run_used_is_a_test_mistake(project: Project, fake_openai: Fake) -> None:
    graph(project, fault="")
    result = project.test(fake_openai(["acme_get_order", "acme_answer"]))
    assert result.code == 2, result.out + result.err
    assert "the mock on tool:get_stock was never used" in result.out
    assert "the tools seen are check_inventory, get_order, " in result.out


@pytest.mark.design("labels.siblings_end_as_stopped")
def test_runs_stopped_by_a_bad_label_end_as_stopped(project: Project, fake_openai: Fake) -> None:
    """The lone first run reports nothing, so the check falls to a run beside others."""
    graph(project, "first_goes_quiet", TIMEOUT.replace("tool:get_order", "tool:get_ordr"), 3)
    project.edit("parallel = 1", "parallel = 2")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    project.edit("acme.graph:first_goes_quiet", "acme.slow:first_goes_quiet")
    result = project.test()
    outcomes = sorted(str(s["outcome"]) for s in result.summaries())
    assert outcomes == ["answered", "infrastructure error", "stopped"], result.out
    assert "tool:get_ordr names no tool the agent has" in result.out


@pytest.mark.design("labels.check_survives_a_quiet_run")
@pytest.mark.parametrize("outcome", ["timed out", "crashed", "infrastructure error"])
def test_a_first_run_that_reports_nothing_leaves_the_check_to_a_later_run(
    project: Project, outcome: str
) -> None:
    graph(project, fault=TIMEOUT.replace("tool:get_order", "tool:get_ordr"))
    cfg = config.load(project.toml)
    runner = Runner(cfg)
    row = plan.rows(cfg)[0]
    try:
        runner.check_labels(RunResult("run-1", row, outcome, None, None, {}))
        assert runner.tools_seen is None and not runner.cancelled
        runner.check_labels(
            RunResult(
                "run-2", row, "answered", "hi", None, {}, tools_seen=["get_order", "get_stock"]
            )
        )
    finally:
        runner.server.server.server_close()
    assert runner.stopped_by == "unknown_label" and runner.cancelled
    assert len(runner.label_problems) == 1 and "tool:get_ordr" in runner.label_problems[0]


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
    project.edit(STOCK_MOCK, "")
    graph(project, "report_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    result = project.test()
    assert result.code == code, result.out + result.err
    assert result.summaries()[0]["answer"] == answer


@pytest.mark.design("adapter.plumbing_not_recorded")
def test_langchains_own_arguments_are_left_out_of_the_record(project: Project) -> None:
    graph(project, "plumbing", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:*"]')
    (lines,) = project.test().records()
    calls = {str(x["call"]): x for x in tool_lines(lines)}
    args = calls["tool:takes_config"]["args"]
    assert isinstance(args, dict)
    assert args["preview"] == '{"sku": "SKU-42"}'
    named = calls["tool:named_config"]["args"]
    assert isinstance(named, dict) and named["preview"] == '{"config": "kept"}'


@pytest.mark.design("adapter.async_real_error_recorded")
def test_a_real_error_in_an_async_tool_is_recorded(project: Project) -> None:
    graph(project, "async_tool", "")
    project.edit('answer   = "messages[-1].content"\n', "")
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:*"]')
    (lines,) = project.test().records()
    assert str(tool_lines(lines)[0]["error"]).startswith("RuntimeError: the real orders")


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
    assert "offered delete_all, which no gate sees" in result.out


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
    own = {k: v for k, v in vars(Shape).items() if k not in ("__dict__", "__weakref__")}
    return type("BaseTool", (), own)


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


@pytest.mark.design("adapter.poisoned_run")
def test_after_a_refusal_every_later_tool_fails_at_once() -> None:
    sink = Sink()
    plan = Plan("r", "t", "baseline", (), (), (), ("tool:safe",), 60.0)
    run = state.RunState(plan, sink, [])
    state.install(run)
    base = base_tool()
    ran: list[str] = []
    try:
        langchain_gate.patch(module(BaseTool=base, ToolException=Exception), run, pytest.fail)

        class Named(base):  # type: ignore[valid-type, misc]
            def _run(self, *args: Any, **kwargs: Any) -> str:
                ran.append(self.name)
                return "real"

        with pytest.raises(FaultlineRefused, match="neither mocked"):
            Named(name="write").run({})
        with pytest.raises(FaultlineRefused, match="already refused"):
            Named(name="safe").run({})
    finally:
        state.install(None)
    assert ran == []
    assert [x["action"] for x in sink.lines] == ["refused", "refused"]


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


def offered(
    project: Project, function: str, module: str = "acme.unsafe"
) -> tuple[Result, FakeOpenAI]:
    """Run an agent from module against a model that calls both tools."""
    graph(project, function, "")
    project.edit(f"acme.graph:{function}", f"{module}:{function}")
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        return project.test(fake), fake
    finally:
        fake.stop()


def wrote(result: Result) -> bool:
    return (result.root / "wrote").exists()


@pytest.mark.design("gate.wrapper_is_not_the_gate")
def test_a_wrapper_copying_a_gates_attributes_is_gated_itself(project: Project) -> None:
    result, _ = offered(project, "wrapped_gate")
    assert result.code == 0, result.out + result.err
    assert not wrote(result)
    assert [x["action"] for x in tool_lines(result.records()[0])] == ["mock", "mock"]


@pytest.mark.design("gate.wrapper_is_not_the_gate")
@pytest.mark.parametrize("kind", ["sync", "async"])
def test_only_the_gate_itself_passes_for_a_gate(kind: str) -> None:
    def sync_tool() -> str:
        return "real"

    async def async_tool() -> str:
        return "real"

    gated = tool_gate.gate(sync_tool if kind == "sync" else async_tool, "t")

    @functools.wraps(gated)
    def wrapper() -> str:
        return "wrote"

    assert tool_gate.is_gate(gated) and not tool_gate.is_gate(wrapper)
    shim = module(f=wrapper)
    tool_gate.swap(shim, ["f"])
    assert shim.f is not wrapper


@pytest.mark.design("adapter.listed_tool_must_pass")
def test_listing_a_tool_does_not_vouch_for_one_that_skips_the_gate(project: Project) -> None:
    project.edit(
        "[agent]\n", '[agent]\ntools = ["acme.listed:write_order", "acme.listed:get_stock"]\n'
    )
    project.edit('real     = ["tool:check_inventory"]', "real = []")
    project.edit('call    = "tool:get_order"', 'call    = "tool:write_order"')
    result, fake = offered(project, "answer", "acme.listed")
    assert result.code == 2, result.out + result.err
    assert not wrote(result) and fake.requests == []
    assert "offered write_order, which no gate sees" in result.out


def stopped_at_the_offer(result: Result, fake: FakeOpenAI) -> None:
    assert result.code == 2, result.out + result.err
    assert not wrote(result) and fake.requests == []


@pytest.mark.design("adapter.blocked_name_wins")
def test_a_gated_tool_does_not_vouch_for_a_second_tool_of_its_name(project: Project) -> None:
    result, fake = offered(project, "same_name_as_a_gated_tool")
    stopped_at_the_offer(result, fake)
    assert "get_order cannot be gated, because its class overrides invoke" in result.out


@pytest.mark.design("adapter.run_on_the_tool_refused")
@pytest.mark.parametrize(
    "function",
    ["run_set_on_the_tool", "run_set_on_the_tool_called_directly", "run_set_when_made"],
)
def test_a_run_function_set_on_the_tool_itself_never_runs(project: Project, function: str) -> None:
    result, _ = offered(project, function)
    assert result.code == 2, result.out + result.err
    assert not wrote(result)
    assert "_run is set on the tool itself" in result.out
