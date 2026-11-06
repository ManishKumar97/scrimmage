"""Tools the model is offered: hosted writes, unknown types and tools no gate sees."""

from collections.abc import Callable

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from faultline_sdk import provider_openai as table

Fake = Callable[..., FakeOpenAI]
TOOL_FAULT = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'


def use(project: Project, function: str) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "{function}"\nruns = 1\nparallel = 1',
    )
    project.edit(TOOL_FAULT, "")


@pytest.mark.design("hosted.write_never_sent")
def test_an_unlisted_hosted_mcp_server_is_never_sent(project: Project) -> None:
    use(project, "acme.rag:answer_hosted")
    fake = FakeOpenAI([], response="response_text")
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert fake.paths == []
    assert result.code == 2
    assert 'add "hosted:mcp/orders" to [agent] real' in result.out
    refused = next(x for x in result.records()[0] if x.get("action") == "refused")
    assert refused["reason"] == "hosted"


@pytest.mark.design("hosted.listed_runs")
def test_a_listed_hosted_mcp_server_is_sent(project: Project, fake_openai: Fake) -> None:
    use(project, "acme.rag:answer_hosted")
    project.edit(
        'real     = ["tool:check_inventory"]',
        'real     = ["tool:check_inventory", "hosted:mcp/orders"]',
    )
    fake = fake_openai([], response="response_text")
    result = project.test(fake)
    assert result.code == 0, result.out
    assert fake.paths == ["/v1/responses"]


@pytest.mark.design("hosted.unknown_type_stops")
def test_an_unknown_tool_type_stops_the_test_naming_it(project: Project) -> None:
    use(project, "acme.rag:answer_odd_tool")
    fake = FakeOpenAI([])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert fake.paths == []
    assert result.code == 2
    assert "a tool of type teleport" in result.out


@pytest.mark.design("hosted.unseen_tool_stops")
@pytest.mark.parametrize("function", ["acme.rag:answer_unseen", "acme.rag:answer_old_functions"])
def test_a_tool_no_gate_sees_stops_the_test_before_any_model_call(
    project: Project, function: str
) -> None:
    use(project, function)
    fake = FakeOpenAI(["chat_answer"])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert fake.paths == []  # nothing reached the model
    assert result.code == 2
    assert "test mistake" in result.out
    assert "lookup_order" in result.out and '"package.module:function"' in result.out


RENAMED = '{path = "acme.tools:get_order", name = "lookup-order"}'


@pytest.mark.design("hosted.renamed_tool_is_seen")
@pytest.mark.parametrize(
    ("function", "reply", "path"),
    [
        ("acme.rag:answer_renamed", {}, "/v1/chat/completions"),
        ("acme.rag:answer_renamed_responses", {"response": "response_text"}, "/v1/responses"),
    ],
)
def test_a_renamed_tool_counts_as_seen(
    project: Project, fake_openai: Fake, function: str, reply: dict[str, str], path: str
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "{function}"\nruns = 1\nparallel = 1',
    )
    project.edit('"acme.tools:get_order"', RENAMED)
    project.edit('call    = "tool:get_order"', 'call    = "tool:lookup-order"')
    project.edit('call = "tool:get_order"', 'call = "tool:lookup-order"')
    fake = fake_openai([] if reply else ["chat_answer", "chat_answer"], **reply)
    result = project.test(fake)
    assert result.code == 0, result.out + result.err
    assert path in fake.paths
    fault = next(lines for lines in result.records() if lines[-1]["row"] == "fault 1")
    tool = next(x for x in fault if x.get("call") == "tool:lookup-order")
    assert tool["action"] == "fault"


@pytest.mark.design("hosted.renamed_tool_is_seen")
def test_the_old_name_of_a_renamed_tool_is_not_seen(project: Project) -> None:
    project.edit('"acme.tools:get_order"', RENAMED)
    project.edit('call    = "tool:get_order"', 'call    = "tool:lookup-order"')
    project.edit('call = "tool:get_order"', 'call = "tool:lookup-order"')
    project.edit("[agent]\n", "[agent]\nruns = 1\nparallel = 1\n")
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert fake.paths == []
    assert "the model would be offered get_order, which no gate sees" in result.out


@pytest.mark.design("hosted.reads_noted")
def test_a_provider_tool_that_only_reads_is_noted_and_warned_about(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer_web_search")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 0, result.out
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["hosted_reads"] == ["web_search"]
    assert "1 model request offered provider tools that only read" in result.out


def test_every_tool_the_gates_see_passes(project: Project, fake_openai: Fake) -> None:
    use(project, "acme.rag:answer")
    result = project.test(fake_openai(ACME, embedding="embedding"))
    assert result.code == 0, result.out


@pytest.mark.design("hosted.table")
@pytest.mark.parametrize(
    ("path", "tool", "agent", "writes", "unknown"),
    [
        ("/v1/responses", {"type": "web_search"}, [], [], []),
        ("/v1/responses", {"type": "code_interpreter"}, [], [], []),
        ("/v1/responses", {"type": "tool_search", "execution": "server"}, [], [], []),
        (
            "/v1/responses",
            {"type": "tool_search", "execution": "client", "name": "s"},
            ["s"],
            [],
            [],
        ),
        ("/v1/responses", {"type": "mcp", "server_label": "orders"}, [], ["orders"], []),
        ("/v1/responses", {"type": "function", "name": "f"}, ["f"], [], []),
        ("/v1/responses", {"type": "computer", "name": "c"}, ["c"], [], []),
        (
            "/v1/responses",
            {"type": "namespace", "tools": [{"type": "function", "name": "inner"}]},
            ["inner"],
            [],
            [],
        ),
        ("/v1/responses", {"type": "brand_new"}, [], [], ["brand_new"]),
        ("/v1/chat/completions", {"type": "function", "function": {"name": "f"}}, ["f"], [], []),
        ("/v1/chat/completions", {"type": "custom", "custom": {"name": "c"}}, ["c"], [], []),
        ("/v1/chat/completions", {"type": "brand_new"}, [], [], ["brand_new"]),
    ],
)
def test_the_tool_table(
    path: str,
    tool: dict[str, object],
    agent: list[str],
    writes: list[str],
    unknown: list[str],
) -> None:
    found = table.offered(path, {"tools": [tool]})
    assert (found.agent, found.hosted_writes, found.unknown) == (agent, writes, unknown)


@pytest.mark.design("model.addresses")
@pytest.mark.parametrize(
    ("path", "method", "op"),
    [
        ("/v1/chat/completions", "POST", "chat"),
        ("/openai/v1/chat/completions/", "POST", "chat"),
        ("/v1/responses", "POST", "chat"),
        ("/v1/chat/completions", "GET", None),
        ("/v1/embeddings", "POST", "embeddings"),
        ("/v1/moderations", "POST", "moderations"),
        ("/v1/responses/input_tokens", "POST", "input_tokens"),
        ("/v1/fine_tuning/jobs", "POST", None),
        ("/v1/files", "POST", None),
    ],
)
def test_model_addresses_match_by_path_and_method(path: str, method: str, op: str | None) -> None:
    assert table.op_for(path, method) == op


def test_web_search_options_and_old_functions_are_read_on_chat() -> None:
    found = table.offered(
        "/v1/chat/completions",
        {"web_search_options": {}, "functions": [{"name": "old"}]},
    )
    assert (found.agent, found.hosted_reads) == (["old"], ["web_search"])


def test_error_bodies_have_the_shape_the_library_reads() -> None:
    assert b"rate_limit_exceeded" in table.error_body(429)
    assert b"server_error" in table.error_body(599)
    assert b"invalid_request_error" in table.error_body(418)
