"""The order bot on the OpenAI Agents SDK: the team writes no tools list, the adapter finds the
function tools, and a handoff is seen and recorded, never refused."""

from collections.abc import Callable
from pathlib import Path

import pytest

import fakes
from conftest import EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage import agents_gate

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


# The unit tests below patch stand-ins, so no library is imported.


def test_arguments_are_parsed_from_the_models_json_and_bad_text_matches_no_args() -> None:
    assert agents_gate.parsed('{"order_id": "1234"}') == {"order_id": "1234"}
    assert agents_gate.parsed("") == {}
    assert agents_gate.parsed("not json") == {"<unparsed arguments>": "not json"}
    assert agents_gate.parsed("[1, 2]") == {"<unparsed arguments>": "[1, 2]"}
