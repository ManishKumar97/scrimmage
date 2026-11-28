"""The order bot on the OpenAI Agents SDK: the team writes no tools list, the adapter finds the
function tools, and a handoff is seen and recorded, never refused."""

import asyncio
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import fakes
from conftest import EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage import agents_gate, errors, http_gate, state, tool_gate
from scrimmage.plan import Plan

Fake = Callable[..., FakeOpenAI]
TOOLS_LINE = (
    'tools    = ["acme.tools:get_order", "acme.tools:check_inventory", '
    '"acme.tools:refund_order", "acme.tools:get_stock"]\n'
)
TURNS = ["agents_get_order", "agents_get_stock", "agents_answer"]
DEFAULT_TEXT = "An error occurred while running the tool."


def bot(project: Project, function: str = "answer", fault: str = "") -> None:
    """The S1 file without its tools line, on the Agents SDK bot."""
    project.edit(TOOLS_LINE, "")
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "acme.agents_bot:{function}"\nruns     = 1\nparallel = 1',
    )
    project.edit(EXAMPLE_FAULT, fault)


def fault(do: str = "error", extra: str = "", call: str = "get_order") -> str:
    return f'[[test.fault]]\ncall = "tool:{call}"\ndo   = "{do}"\nforbid = ["never said"]\n{extra}'


def row(result: Result, name: str) -> list[dict[str, object]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)


def tool_lines(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    return [x for x in lines if x.get("kind") == "tool"]


def outcome(lines: list[dict[str, object]]) -> object:
    return lines[-1]["summary"]["outcome"]  # type: ignore[index]


def told(fake: FakeOpenAI) -> list[str]:
    """What the Agents SDK told the model about each tool call, from the requests it sent."""
    return [
        str(item["output"])
        for request in fake.requests
        for item in request["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


@pytest.mark.design("adapter.agents_no_tools_list")
@pytest.mark.slow
def test_function_tools_run_without_a_tools_list(project: Project, fake_openai: Fake) -> None:
    bot(project)
    result = project.test(fake_openai([], responses=TURNS))
    assert result.code == 0, result.out + result.err
    base = row(result, "baseline")
    assert [(x["call"], x["action"]) for x in tool_lines(base)] == [
        ("tool:get_order", "mock"),
        ("tool:get_stock", "mock"),
    ]
    assert base[-1]["summary"]["answer"] == (  # type: ignore[index]
        "Order 1234 shipped on Oct 30. SKU-42 has 12 in stock."
    )


@pytest.mark.design("adapter.agents_typed_mock")
@pytest.mark.slow
def test_a_mock_becomes_the_type_the_tool_function_returns(
    project: Project, fake_openai: Fake
) -> None:
    bot(project)
    fake = fake_openai([], responses=TURNS)
    project.test(fake)
    assert any("Stock(" in text for text in told(fake)), told(fake)


@pytest.mark.design("adapter.agents_error_default_failure")
@pytest.mark.slow
def test_an_error_fault_reaches_the_model_as_a_real_failure_does(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, fault=fault())
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    run = row(result, "fault 1")
    line = tool_lines(run)[0]
    assert (line["call"], line["action"]) == ("tool:get_order", "fault")
    assert str(line["error"]).startswith("RuntimeError: get_order failed")
    assert outcome(run) == "answered"
    assert DEFAULT_TEXT in told(fake)  # the SDK's own text, from its failure-error function
    assert not any("real orders system" in text for text in told(fake))


@pytest.mark.design("adapter.agents_error_custom_failure")
@pytest.mark.slow
def test_an_error_fault_goes_through_the_tools_own_failure_function(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_told", fault())
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    assert outcome(row(result, "fault 1")) == "answered"
    assert "the orders system is down" in told(fake)
    assert DEFAULT_TEXT not in told(fake)


@pytest.mark.design("adapter.agents_error_no_handler")
@pytest.mark.slow
def test_an_error_fault_with_no_failure_function_ends_the_run_as_crashed(
    project: Project,
) -> None:
    bot(project, "answer_raises", fault())
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.finished, result.out + result.err
    run = row(result, "fault 1")
    assert outcome(run) == "crashed"
    assert str(tool_lines(run)[0]["error"]).startswith("RuntimeError: get_order failed")


@pytest.mark.design("fault.agents_error_raises")
@pytest.mark.slow
def test_a_fault_can_name_another_error_type(project: Project, fake_openai: Fake) -> None:
    bot(project, fault=fault(extra='raises = "ConnectionError"\n'))
    result = project.test(fake_openai([], responses=TURNS))
    line = tool_lines(row(result, "fault 1"))[0]
    assert str(line["error"]).startswith("ConnectionError: get_order failed")


@pytest.mark.design("adapter.agents_timeout_fault")
@pytest.mark.slow
def test_a_timeout_fault_goes_through_the_failure_function(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, fault=fault("timeout"))
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    line = tool_lines(row(result, "fault 1"))[0]
    assert str(line["error"]).startswith("TimeoutError: get_order timed out")
    assert DEFAULT_TEXT in told(fake)


@pytest.mark.design("adapter.agents_empty_fault")
@pytest.mark.slow
def test_an_empty_fault_returns_the_empty_value_of_the_return_type(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, fault=fault("empty"))
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    line = tool_lines(row(result, "fault 1"))[0]
    assert (line["action"], line.get("error")) == ("fault", None)
    assert "" in told(fake)


@pytest.mark.design("adapter.agents_change_faults")
@pytest.mark.parametrize(
    ("do", "extra", "text"),
    [
        ("append", 'text = " NOTE"\n', "shipped, Oct 30 NOTE"),
        ("replace", 'text = "lost"\n', "lost"),
    ],
)
@pytest.mark.slow
def test_append_and_replace_change_what_the_model_is_told(
    project: Project, fake_openai: Fake, do: str, extra: str, text: str
) -> None:
    bot(project, fault=fault(do, extra))
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    assert tool_lines(row(result, "fault 1"))[0]["action"] == "fault"  # under a mock
    assert text in told(fake)


@pytest.mark.design("adapter.agents_slow_fault")
@pytest.mark.slow
def test_a_slow_fault_longer_than_the_tools_timeout_takes_the_sdks_timeout_path(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_timed", fault("slow 3s"))
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    assert outcome(row(result, "fault 1")) == "answered"
    assert "Tool 'get_order' timed out after 0.5 seconds." in told(fake)


@pytest.mark.design("adapter.agents_slow_fault")
@pytest.mark.slow
def test_a_slow_fault_inside_the_timeout_only_delays_the_call(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_timed", fault("slow 0.1s"))
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    run = row(result, "fault 1")
    assert tool_lines(run)[0]["action"] == "fault"  # under a mock
    assert "shipped, Oct 30" in told(fake)


@pytest.mark.design("adapter.agents_args_mock")
@pytest.mark.slow
def test_a_mock_can_match_the_arguments_the_model_sent(project: Project, fake_openai: Fake) -> None:
    bot(project)
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n',
        '[[test.mock]]\ncall    = "tool:get_order"\nargs    = {order_id = "1234"}\n'
        'returns = "shipped, Oct 30"\n',
    )
    result = project.test(fake_openai([], responses=TURNS))
    assert result.code == 0, result.out + result.err
    assert [x["action"] for x in tool_lines(row(result, "baseline"))] == ["mock", "mock"]


@pytest.mark.design("adapter.agents_args_mock")
@pytest.mark.slow
def test_arguments_no_mock_matches_are_refused(project: Project, fake_openai: Fake) -> None:
    bot(project)
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n',
        '[[test.mock]]\ncall    = "tool:get_order"\nargs    = {order_id = "9999"}\n'
        'returns = "shipped, Oct 30"\n',
    )
    result = project.test(fake_openai([], responses=["agents_get_order"]))
    first = tool_lines(result.records()[0])[0]
    assert first["action"] == "refused"
    assert "real orders system" not in str(first["error"])


@pytest.mark.design("adapter.agents_real")
@pytest.mark.slow
def test_a_tool_listed_under_real_runs_for_real(project: Project, fake_openai: Fake) -> None:
    bot(project)
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:check_inventory"]')
    fake = fake_openai([], responses=["agents_check_inventory", "agents_answer"])
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock'
        ' object\nreturns = {sku = "SKU-42", count = 12}\n',
        "",
    )
    result = project.test(fake)
    line = tool_lines(row(result, "baseline"))[0]
    assert (line["call"], line["action"]) == ("tool:check_inventory", "real")
    assert "SKU-42: 12 in stock" in told(fake)


@pytest.mark.design("adapter.agents_real")
@pytest.mark.slow
def test_a_real_failure_and_an_injected_error_reach_the_model_alike(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, fault=fault())
    injected_fake = fake_openai([], responses=TURNS)
    project.test(injected_fake)

    project.edit(fault(), "")
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:get_order"]')
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    real_fake = fake_openai([], responses=TURNS)
    real = project.test(real_fake)
    line = tool_lines(real.records()[-1])[0]
    assert line["action"] == "real"
    assert str(line["error"]).startswith("RuntimeError: the real orders system")
    assert {t for t in told(real_fake) if "error" in t} == {
        t for t in told(injected_fake) if "error" in t
    }


@pytest.mark.design("adapter.agents_refused")
@pytest.mark.slow
def test_a_tool_neither_mocked_nor_real_is_refused_and_never_runs(
    project: Project, fake_openai: Fake
) -> None:
    bot(project)
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    fake = fake_openai([], responses=TURNS[:1])
    result = project.test(fake)
    lines = result.records()[0]
    first = tool_lines(lines)[0]
    assert (first["call"], first["action"]) == ("tool:get_order", "refused")
    assert "neither mocked" in str(first["error"])
    assert "real orders system" not in str(first["error"])  # the team's code never ran


@pytest.mark.design("adapter.agents_sync_tool")
@pytest.mark.slow
def test_a_plain_function_tool_takes_a_fault_and_a_mock(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_sync", fault())
    fake = fake_openai([], responses=TURNS)
    result = project.test(fake)
    assert [x["action"] for x in tool_lines(row(result, "baseline"))] == ["mock", "mock"]
    line = tool_lines(row(result, "fault 1"))[0]
    assert (line["action"], str(line["error"])[:25]) == ("fault", "RuntimeError: get_order f")
    assert DEFAULT_TEXT in told(fake)


@pytest.mark.design("adapter.agents_label_is_model_name")
@pytest.mark.slow
def test_a_label_uses_the_name_the_model_sees(project: Project, fake_openai: Fake) -> None:
    project.edit('call    = "tool:get_order"', 'call    = "tool:lookup_order"')
    bot(project, "answer_renamed")
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock'
        ' object\nreturns = {sku = "SKU-42", count = 12}\n',
        "",
    )
    project.edit('real     = ["tool:check_inventory"]', "real = []")
    fake = FakeOpenAI([], responses=["agents_get_order_renamed", "agents_answer"])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert tool_lines(result.records()[0])[0]["call"] == "tool:lookup_order"


@pytest.mark.design("adapter.agents_handoff")
@pytest.mark.slow
def test_a_handoff_is_seen_and_recorded_and_never_refused(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_handoff")
    project.edit('expect   = ["shipped", ["Oct 30", "October 30"]]', 'expect   = ["paid"]')
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock'
        ' object\nreturns = {sku = "SKU-42", count = 12}\n',
        "",
    )
    project.edit('real     = ["tool:check_inventory"]', "real = []")
    fake = fake_openai([], responses=["agents_handoff", "agents_billing_answer"])
    result = project.test(fake)
    assert result.code == 0, result.out + result.err
    run = row(result, "baseline")
    switched = [x for x in tool_lines(run) if x["call"] == "handoff:transfer_to_billing_agent"]
    assert len(switched) == 1 and switched[0]["action"] == "real", tool_lines(run)
    assert run[-1]["summary"]["answer"] == "Billing here: order 1234 was paid."  # type: ignore[index]


@pytest.mark.design("adapter.agents_handoff")
@pytest.mark.slow
def test_a_handoff_beside_a_function_tool_runs_a_baseline(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "answer_handoff_with_tool")
    project.edit('expect   = ["shipped", ["Oct 30", "October 30"]]', 'expect   = ["paid"]')
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock'
        ' object\nreturns = {sku = "SKU-42", count = 12}\n',
        "",
    )
    project.edit('real     = ["tool:check_inventory"]', "real = []")
    fake = fake_openai(
        [], responses=["agents_get_order", "agents_handoff", "agents_billing_answer"]
    )
    result = project.test(fake)
    assert result.code == 0, result.out + result.err
    calls = [(x["call"], x["action"]) for x in tool_lines(row(result, "baseline"))]
    assert calls == [
        ("tool:get_order", "mock"),
        ("handoff:transfer_to_billing_agent", "real"),
    ]


@pytest.mark.design("adapter.agents_unseen_tool")
@pytest.mark.slow
def test_a_tool_with_its_own_invoker_is_refused_by_the_model_gate_with_the_reason(
    project: Project,
) -> None:
    bot(project, "answer_custom")
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert "its on_invoke_tool is the team's own function" in result.out + result.err
    assert "real orders system" not in result.out + result.err
    assert not fake.requests  # refused before it was sent


@pytest.mark.design("adapter.agents_unseen_tool")
@pytest.mark.slow
def test_an_invoker_replaced_after_the_tool_was_made_is_refused_at_the_model_request(
    project: Project,
) -> None:
    bot(project, "answer_replaced")
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert "its on_invoke_tool is the team's own function" in result.out + result.err
    assert not fake.requests


SERVER = Path(fakes.__file__).parent / "docs_server.py"
SEARCH = "mcp:docs/search"


def mcp_project(project: Project, body: str) -> Path:
    """The Agents SDK bot with its tools from the documents MCP server, and the server's log."""
    log = project.root / "received.log"
    project.monkeypatch.setenv("DOCS_SERVER", str(SERVER))
    project.monkeypatch.setenv("DOCS_LOG", str(log))
    project.monkeypatch.setenv("DOCS_MODE", "ok")
    project.toml.write_text(
        '[agent]\nfunction = "acme.agents_bot:answer_mcp"\nruns = 1\nparallel = 1\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'
        '[[test]]\nname = "docs"\nquestion = "Find refunds"\nexpect = ["x"]\n' + body
    )
    return log


@pytest.mark.design("adapter.agents_mcp_once")
@pytest.mark.slow
def test_a_function_tool_made_from_an_mcp_tool_is_gated_once_by_the_mcp_gate(
    project: Project, fake_openai: Fake
) -> None:
    log = mcp_project(
        project, f'[[test.mock]]\ncall = "{SEARCH}"\nreturns = {{hits = ["mocked"], total = 1}}\n'
    )
    fake = fake_openai([], responses=["agents_search", "agents_answer"])
    result = project.test(fake)
    lines = result.records()[0]
    assert [(x["call"], x["action"]) for x in lines if x.get("kind") in ("tool", "mcp")] == [
        (SEARCH, "mock")
    ], lines
    labels = [str(x["call"]) for x in tool_lines(lines)]
    assert "tool:search" not in labels  # not a second decision on the same call
    assert not log.exists() or "search" not in log.read_text()  # the server never saw it
    assert any("mocked" in text for text in told(fake)), told(fake)


@pytest.mark.design("adapter.agents_mcp_once")
@pytest.mark.slow
def test_an_error_fault_on_an_mcp_tool_counts_once(project: Project, fake_openai: Fake) -> None:
    mcp_project(
        project,
        f'[[test.mock]]\ncall = "{SEARCH}"\nreturns = {{hits = ["mocked"], total = 1}}\n'
        f'[[test.fault]]\ncall = "{SEARCH}"\ndo = "error"\nforbid = ["never"]\n',
    )
    result = project.test(fake_openai([], responses=["agents_search", "agents_answer"]))
    run = row(result, "fault 1")
    assert [x["call"] for x in run if x.get("action") == "fault"] == [SEARCH]
    assert run[-1]["summary"]["fault_fired"] is True  # type: ignore[index]


@pytest.mark.design("adapter.agents_unseen_tool")
@pytest.mark.slow
def test_an_implementation_replaced_after_the_tool_was_made_is_refused_with_the_reason(
    project: Project,
) -> None:
    bot(project, "answer_impl_replaced")
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert "its _invoke_tool_impl was replaced" in result.out + result.err
    assert not fake.requests


def touched(project: Project) -> str:
    path = project.root / "touched.log"
    project.monkeypatch.setenv("ACME_TOUCH", str(path))
    return str(path)


def what_ran(path: str) -> str:
    return Path(path).read_text() if Path(path).exists() else ""


def inprocess(project: Project, body: str) -> str:
    path = touched(project)
    project.toml.write_text(
        '[agent]\nfunction = "acme.agents_bot:answer_inprocess_mcp"\nruns = 1\nparallel = 1\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'
        '[[test]]\nname = "docs"\nquestion = "Find refunds"\nexpect = ["x"]\n' + body
    )
    return path


@pytest.mark.design("adapter.agents_mcp_own_server")
@pytest.mark.slow
def test_a_tool_from_an_mcp_server_of_the_teams_own_is_refused_unless_listed(
    project: Project, fake_openai: Fake
) -> None:
    ran = inprocess(project, "")
    result = project.test(fake_openai([], responses=["agents_search"]))
    line = tool_lines(result.records()[0])[0]
    assert (line["call"], line["action"]) == ("tool:search", "refused")
    assert what_ran(ran) == ""  # the server's call_tool never ran


@pytest.mark.design("adapter.agents_mcp_own_server")
@pytest.mark.slow
def test_a_tool_from_an_mcp_server_of_the_teams_own_can_be_mocked(
    project: Project, fake_openai: Fake
) -> None:
    ran = inprocess(project, '[[test.mock]]\ncall = "tool:search"\nreturns = "mocked docs"\n')
    fake = fake_openai([], responses=["agents_search", "agents_answer"])
    result = project.test(fake)
    lines = result.records()[0]
    assert [(x["call"], x["action"]) for x in tool_lines(lines)] == [("tool:search", "mock")]
    assert what_ran(ran) == ""
    assert "mocked docs" in "".join(told(fake))


@pytest.mark.design("adapter.agents_mcp_own_server")
@pytest.mark.slow
def test_a_tool_from_an_mcp_server_of_the_teams_own_runs_when_listed_real(
    project: Project, fake_openai: Fake
) -> None:
    ran = inprocess(project, "")
    project.edit("runs = 1\n", 'runs = 1\nreal = ["tool:search"]\n')
    result = project.test(fake_openai([], responses=["agents_search", "agents_answer"]))
    assert [x["action"] for x in tool_lines(result.records()[0])] == ["real"]
    assert what_ran(ran) == "server\n"


def handoff_project(project: Project, real: str = "") -> str:
    ran = touched(project)
    bot(project, "answer_handoff_callback")
    project.edit('expect   = ["shipped", ["Oct 30", "October 30"]]', 'expect   = ["paid"]')
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit(
        '[[test.mock]]\ncall    = "tool:get_stock"             # returns an acme.models.Stock'
        ' object\nreturns = {sku = "SKU-42", count = 12}\n',
        "",
    )
    project.edit('real     = ["tool:check_inventory"]', f"real = [{real}]")
    return ran


@pytest.mark.design("adapter.agents_handoff_callback")
@pytest.mark.slow
def test_a_handoff_with_a_callback_is_refused_unless_listed_real(
    project: Project, fake_openai: Fake
) -> None:
    ran = handoff_project(project)
    result = project.test(fake_openai([], responses=["agents_handoff"]))
    line = tool_lines(result.records()[0])[0]
    assert (line["call"], line["action"]) == ("handoff:transfer_to_billing_agent", "refused")
    assert 'add "handoff:transfer_to_billing_agent" to real' in str(line["error"])
    assert what_ran(ran) == ""  # the team's callback never ran


@pytest.mark.design("adapter.agents_handoff_callback")
@pytest.mark.slow
def test_a_handoff_with_a_callback_runs_it_when_listed_real(
    project: Project, fake_openai: Fake
) -> None:
    ran = handoff_project(project, '"handoff:transfer_to_billing_agent"')
    result = project.test(fake_openai([], responses=["agents_handoff", "agents_billing_answer"]))
    assert result.code == 0, result.out + result.err
    line = tool_lines(row(result, "baseline"))[0]
    assert (line["call"], line["action"]) == ("handoff:transfer_to_billing_agent", "real")
    assert what_ran(ran) == "callback\n"


@pytest.mark.design("adapter.agents_handoff_callback")
@pytest.mark.slow
def test_a_mock_or_a_fault_cannot_name_a_handoff(project: Project) -> None:
    handoff_project(project)
    project.toml.write_text(
        project.toml.read_text()
        + '[[test.mock]]\ncall = "handoff:transfer_to_billing_agent"\nreturns = "x"\n'
    )
    result = project.test()
    assert result.code != 0
    assert "handoff:transfer_to_billing_agent" in result.out + result.err


@pytest.mark.design("adapter.agents_handoff_name_collision")
@pytest.mark.slow
def test_a_tool_named_like_a_handoff_never_runs_in_its_place(
    project: Project, fake_openai: Fake
) -> None:
    """The SDK routes a call to the handoff first, so the tool of the same name is never run."""
    ran = touched(project)
    bot(project, "answer_handoff_collision")
    project.edit('expect   = ["shipped", ["Oct 30", "October 30"]]', 'expect   = ["paid"]')
    fake = fake_openai([], responses=["agents_handoff", "agents_billing_answer"])
    result = project.test(fake)
    calls = [x["call"] for x in tool_lines(result.records()[0])]
    assert calls == ["handoff:transfer_to_billing_agent"], calls
    assert what_ran(ran) == ""


@pytest.mark.design("adapter.agents_handoff_name_collision")
def test_a_handoff_name_vouches_for_one_offered_tool_and_never_a_second() -> None:
    run, _ = fresh_run()
    run.handoffs.add("wire")
    run.adapted.add("tool")
    assert http_gate.vouched_once(["wire", "tool"], run) == set()
    assert http_gate.vouched_once(["wire", "wire"], run) == {"wire"}
    run.adapted.add("wire")  # a tool of that name that a gate does see
    assert http_gate.vouched_once(["wire", "wire"], run) == set()
    assert "wire" in run.seen


@pytest.mark.design("adapter.agents_own_call")
@pytest.mark.slow
def test_an_invoker_that_replaces_call_is_refused_by_the_model_gate_with_the_reason(
    project: Project,
) -> None:
    ran = touched(project)
    bot(project, "answer_own_call")
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert "its invoker replaces the SDK's __call__" in result.out + result.err
    assert not fake.requests
    assert what_ran(ran) == ""


@pytest.mark.design("adapter.agents_run_ending_errors")
@pytest.mark.slow
def test_a_fault_that_cannot_be_raised_ends_the_run_and_is_not_told_to_the_model(
    project: Project,
) -> None:
    bot(project, fault=fault(extra='raises = "acme.nowhere:Missing"\n'))
    fake = FakeOpenAI([], responses=TURNS)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    line = tool_lines(row(result, "fault 1"))[0]
    assert str(line["error"]).startswith("ScrimmageMockError")
    assert DEFAULT_TEXT not in told(fake)  # no text for the model: the run ended


# The unit tests below patch stand-ins, so no library is imported.


class Seen:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        del keep
        self.lines.append(line)


def fresh_run() -> tuple[state.RunState, Seen]:
    sink = Seen()
    return state.RunState(Plan("r", "t", "baseline", (), (), (), (), 60.0), sink, []), sink


STAND_IN = """
import dataclasses

async def maybe_invoke_function_tool_failure_error_function(*, function_tool, context, error):
    return "formatted"

class _FailureHandlingFunctionToolInvoker:
    def __init__(self, invoke_tool_impl, on_handled_error, *, function_tool=None):
        self._invoke_tool_impl = invoke_tool_impl
        self._function_tool = function_tool

    async def __call__(self, ctx, input):
        try:
            return await self._invoke_tool_impl(ctx, input)
        except Exception as error:
            return await maybe_invoke_function_tool_failure_error_function(
                function_tool=self._function_tool, context=ctx, error=error
            )

@dataclasses.dataclass
class FunctionTool:
    name: str
    on_invoke_tool: object

    def __post_init__(self):
        pass
"""
CUTS = {
    "invoker": ("class _FailureHandlingFunctionToolInvoker:", "class _Gone:"),
    "call": ("return await self._invoke_tool_impl(ctx, input)", "return None"),
    "post_init": ("def __post_init__(self):", "def __other__(self):"),
    "field": ("    on_invoke_tool: object\n", ""),
    "format": (
        "async def maybe_invoke_function_tool_failure_error_function",
        "async def other_function",
    ),
}


def tool_module(**drop: bool) -> types.ModuleType:
    """What the gate expects of agents.tool, with named parts left out."""
    source = STAND_IN
    for name, (old, new) in CUTS.items():
        if drop.get(name):
            assert old in source, old
            source = source.replace(old, new, 1)
    shim = types.ModuleType("agents.tool")
    exec(source, vars(shim))
    return shim


def handoff_module(*, field: bool = True) -> types.ModuleType:
    import dataclasses

    names = ["tool_name", "on_invoke_handoff"] if field else ["on_invoke_handoff"]
    shim = types.ModuleType("agents.handoffs")
    vars(shim)["Handoff"] = dataclasses.make_dataclass("Handoff", names)
    return shim


@pytest.mark.design("adapter.agents_structural_check")
@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ({"invoker": True}, "_FailureHandlingFunctionToolInvoker"),
        ({"call": True}, "_invoke_tool_impl"),
        ({"post_init": True}, "FunctionTool.__post_init__"),
        ({"field": True}, "FunctionTool.on_invoke_tool"),
        ({"format": True}, "maybe_invoke_function_tool_failure_error_function"),
    ],
)
def test_a_library_without_the_tool_classes_stops_the_program_naming_the_symbol(
    missing: dict[str, bool], named: str
) -> None:
    run, sink = fresh_run()
    stopped: list[str] = []
    agents_gate.patch_tools(tool_module(**missing), run, stopped.append)
    assert stopped and named in stopped[0], stopped
    assert "the program is stopped" in stopped[0]
    assert "tested with 0.20 to 0.26" in stopped[0]
    assert sink.lines[0]["notice"] == "structure"


@pytest.mark.design("adapter.agents_structural_check")
def test_a_library_without_a_handoff_name_stops_the_program_naming_the_symbol() -> None:
    run, _ = fresh_run()
    stopped: list[str] = []
    agents_gate.patch_handoffs(handoff_module(field=False), run, stopped.append)
    assert stopped and "Handoff.tool_name" in stopped[0], stopped
    stopped.clear()
    agents_gate.patch_handoffs(types.ModuleType("agents.handoffs"), run, stopped.append)
    assert stopped and "Handoff" in stopped[0]


@pytest.mark.design("adapter.agents_structural_check")
@pytest.mark.slow
def test_a_changed_library_stops_the_run_with_exit_3(project: Project) -> None:
    bot(project)
    project.edit("acme.agents_bot:answer", "acme.agents_probe:answer")
    (project.root / "acme" / "agents_probe.py").write_text(
        "import agents.tool\n\n\ndef answer(question):\n    return 'ran'\n"
    )
    package = project.root / "agents"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "tool.py").write_text("class FunctionTool:\n    pass\n")
    result = project.test()
    assert result.code == 3, result.out + result.err
    assert "_FailureHandlingFunctionToolInvoker" in result.out
    assert "the program is stopped" in result.out


@pytest.mark.design("adapter.agents_tested_range")
def test_a_newer_library_gets_a_notice_and_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_gate, "version", lambda: (99, 0))
    run, sink = fresh_run()
    stopped: list[str] = []
    shim = tool_module()
    agents_gate.patch_tools(shim, run, stopped.append)
    assert not stopped
    assert sink.lines[0]["notice"] == "newer_version"
    assert "openai-agents 99.0 is newer than the versions" in str(sink.lines[0]["message"])
    invoker = vars(shim)["_FailureHandlingFunctionToolInvoker"]
    made = vars(shim)["FunctionTool"](name="later", on_invoke_tool=None)
    assert made.name == "later"
    assert tool_gate.is_gate(invoker(_Impl(), None)._invoke_tool_impl)


async def _impl(ctx: Any, text: str) -> str:
    return "real"


class _Impl:
    """Callable the invoker takes as its implementation."""

    async def __call__(self, ctx: Any, text: str) -> str:
        return "real"


def test_the_invoker_gate_lets_a_call_through_when_no_run_is_going() -> None:
    run, _ = fresh_run()
    shim = tool_module()
    agents_gate.patch_tools(shim, run, pytest.fail)
    invoker = vars(shim)["_FailureHandlingFunctionToolInvoker"](_impl, None)
    assert asyncio.run(invoker(None, "{}")) == "real"


def test_a_callable_object_as_the_implementation_is_gated_and_awaited() -> None:
    plan = Plan("r", "t", "baseline", (), (), (), ("tool:later",), 60.0)
    sink = Seen()
    run = state.RunState(plan, sink, [])
    state.install(run)
    try:
        shim = tool_module()
        agents_gate.patch_tools(shim, run, pytest.fail)
        tool = vars(shim)["FunctionTool"](name="later", on_invoke_tool=None)
        invoker = vars(shim)["_FailureHandlingFunctionToolInvoker"](
            _Impl(), None, function_tool=tool
        )
        assert asyncio.run(invoker(None, "{}")) == "real"
    finally:
        state.install(None)
    assert [(x["call"], x["action"]) for x in sink.lines if x.get("kind") == "tool"] == [
        ("tool:later", "real")
    ]


@pytest.mark.design("adapter.agents_run_ending_errors")
@pytest.mark.parametrize(
    "error",
    [
        errors.ScrimmageTimeLimit("the run's time is up"),
        errors.ScrimmageStopped("the run was stopped"),
        errors.ScrimmageMockError("the mock does not fit"),
    ],
)
def test_an_error_that_ends_the_run_is_never_turned_into_text_for_the_model(
    error: Exception,
) -> None:
    shim = tool_module()
    run, _ = fresh_run()
    agents_gate.patch_tools(shim, run, pytest.fail)

    async def boom(ctx: Any, text: str) -> str:
        raise error

    tool = vars(shim)["FunctionTool"](name="t", on_invoke_tool=None)
    invoker = vars(shim)["_FailureHandlingFunctionToolInvoker"](boom, None, function_tool=tool)
    with pytest.raises(type(error)):
        asyncio.run(invoker(None, "{}"))


@pytest.mark.design("adapter.agents_run_ending_errors")
def test_an_ordinary_error_still_goes_to_the_failure_function() -> None:
    shim = tool_module()
    run, _ = fresh_run()
    agents_gate.patch_tools(shim, run, pytest.fail)

    async def boom(ctx: Any, text: str) -> str:
        raise ValueError("a failure of the tool")

    tool = vars(shim)["FunctionTool"](name="t", on_invoke_tool=None)
    invoker = vars(shim)["_FailureHandlingFunctionToolInvoker"](boom, None, function_tool=tool)
    assert asyncio.run(invoker(None, "{}")) == "formatted"


def test_arguments_are_parsed_from_the_models_json_and_bad_text_matches_no_args() -> None:
    assert agents_gate.parsed('{"order_id": "1234"}') == {"order_id": "1234"}
    assert agents_gate.parsed("") == {}
    assert agents_gate.parsed("not json") == {"<unparsed arguments>": "not json"}
    assert agents_gate.parsed("[1, 2]") == {"<unparsed arguments>": "[1, 2]"}
