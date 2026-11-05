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
    assert 'add "hosted:mcp/orders" to [agent] real' in result.out or result.records()
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
def test_a_tool_no_gate_sees_stops_the_test_before_the_second_model_call(
    project: Project,
) -> None:
    use(project, "acme.rag:answer_unseen")
    fake = FakeOpenAI(["chat_answer", "chat_answer"])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert fake.paths == ["/v1/chat/completions"]  # one call went; the second did not
    assert result.code == 2
    assert "lookup_order" in result.out and "[agent] tools" in result.out
    first = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert first["unseen_tools"] == ["lookup_order"]


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


@pytest.mark.parametrize(
    ("path", "op"),
    [
        ("/v1/chat/completions", "chat"),
        ("/openai/v1/chat/completions/", "chat"),
        ("/v1/responses", "chat"),
        ("/v1/embeddings", "embeddings"),
        ("/v1/fine_tuning/jobs", None),
        ("/v1/files", None),
    ],
)
def test_model_addresses_match_by_path(path: str, op: str | None) -> None:
    assert table.op_for(path) == op


def test_error_bodies_have_the_shape_the_library_reads() -> None:
    assert b"rate_limit_exceeded" in table.error_body(429)
    assert b"server_error" in table.error_body(599)
    assert b"invalid_request_error" in table.error_body(418)
