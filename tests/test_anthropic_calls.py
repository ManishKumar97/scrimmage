"""An Anthropic call is recognized as a model call, and the tools it offers are sorted.

recognize: POST /v1/messages and /v1/messages/count_tokens, on Anthropic's host, on a gateway
named under model_hosts, and on a host listed for both providers. tools: which tools the agent
runs, which run at Anthropic and only read, and which can change data."""

from pathlib import Path
from typing import Any

import httpx2
import pytest

from model_gate import Gate, Provider
from scrimmage import config, state
from scrimmage.config import ModelHost
from scrimmage.errors import ConfigError, ScrimmageRefused
from scrimmage.prices import Rates
from scrimmage.providers.impl.wire import wrap_fields
from scrimmage.providers.registry import built_in, registry_for

REPLY = (Path(__file__).parent / "fixtures" / "anthropic" / "doc_answer.json").read_bytes()
COUNT = (Path(__file__).parent / "fixtures" / "anthropic" / "doc_count_tokens.json").read_bytes()
BODY = {"model": "claude-test", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
URL = "https://api.anthropic.com/v1/messages"


def answered(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        200, content=COUNT if request.url.path.endswith("count_tokens") else REPLY
    )


def labelled(run: Gate, url: str, body: dict[str, Any] = BODY) -> list[dict[str, Any]]:
    provider = Provider(run.events, answered)
    client = httpx2.Client(transport=httpx2.MockTransport(provider))
    client.post(url, json=body)
    return run.sink.lines


@pytest.fixture(autouse=True)
def gated() -> Any:
    yield
    state.install(None)


def gate(*hosts: ModelHost, **settings: Any) -> Gate:
    return Gate(hosts=hosts, prices=(("claude-test", Rates(3.0, 15.0)),), **settings)


def test_recognize_messages_on_anthropics_host_with_no_model_hosts() -> None:
    (line,) = labelled(gate(), URL)
    assert line["call"] == "model:anthropic"
    assert line["status"] == 200
    assert line["action"] != "refused"
    assert line["tokens"]["input"] == 12


def test_recognize_count_tokens_as_the_free_input_tokens_op() -> None:
    run = gate()
    (line,) = labelled(run, URL + "/count_tokens")
    assert line["call"] == "model:anthropic/input_tokens"
    assert line["cost_usd"] == 0.0
    assert "reserve" not in run.events


def test_recognize_a_gateway_named_with_provider_anthropic() -> None:
    run = gate(ModelHost("gateway.acme.internal", "anthropic"))
    (line,) = labelled(run, "https://gateway.acme.internal/v1/messages")
    assert line["call"] == "model:anthropic"
    assert line["cost_usd"] is not None


def test_recognize_a_path_of_the_other_provider_on_a_gateway_named_for_anthropic_alone() -> None:
    run = gate(ModelHost("gateway.acme.internal", "anthropic"))
    with pytest.raises(ScrimmageRefused):
        labelled(run, "https://gateway.acme.internal/v1/chat/completions")


def test_recognize_one_host_listed_for_both_providers_picks_by_path() -> None:
    both = (ModelHost("gw.test", "openai"), ModelHost("gw.test", "anthropic"))
    run = gate(*both)
    provider = Provider(run.events, answered)
    client = httpx2.Client(transport=httpx2.MockTransport(provider))
    client.post("https://gw.test/v1/messages", json=BODY)
    client.post("https://gw.test/v1/messages/count_tokens", json=BODY)
    labels = [line["call"] for line in run.sink.lines]
    assert labels == ["model:anthropic", "model:anthropic/input_tokens"]


def test_recognize_a_post_to_a_path_that_is_not_a_model_call_is_a_web_request() -> None:
    run = gate()
    provider = Provider(run.events, answered)
    client = httpx2.Client(transport=httpx2.MockTransport(provider))
    assert client.get(URL).status_code == 200
    with pytest.raises(ScrimmageRefused):
        client.post("https://api.anthropic.com/v1/messages/batches", json={"requests": []})
    assert [line["call"] for line in run.sink.lines] == ["http:api.anthropic.com"] * 2


def test_recognize_the_hint_for_a_model_path_on_an_unnamed_host_names_anthropic() -> None:
    hint = state.model_path_hint("http:guard.internal", "POST /v1/messages")
    assert hint is not None
    assert 'provider = "anthropic"' in hint
    hint = state.model_path_hint("http:guard.internal", "POST /v1/chat/completions")
    assert hint is not None
    assert 'provider = "openai"' in hint
    assert state.model_path_hint("http:guard.internal", "POST /v1/messages/count_tokens")
    assert state.model_path_hint("http:guard.internal", "GET /v1/messages") is None


def test_recognize_real_cannot_list_an_anthropic_path_as_a_web_request(tmp_path: Path) -> None:
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        'real = ["http:guard.internal POST /v1/messages"]\n'
        '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["ok"]\n'
    )
    (tmp_path / "scrimmage.toml").write_text(text)
    with pytest.raises(ConfigError, match="lists a model call as a web request") as caught:
        config.load(tmp_path / "scrimmage.toml")
    assert '{host = "guard.internal", provider = "anthropic"}' in str(caught.value)


def test_recognize_a_probe_tries_every_provider_for_a_host_no_one_named() -> None:
    registry = built_in()
    found = registry.probe("/v1/messages", "POST")
    assert found is not None
    assert (found[0].name, found[1]) == ("anthropic", "chat")
    found = registry.probe("/v1/messages/count_tokens", "POST")
    assert found is not None
    assert (found[0].name, found[1]) == ("anthropic", "input_tokens")
    assert registry.probe("/v1/threads/t/messages", "POST") is None


def test_recognize_the_registry_for_a_config_that_names_anthropic_holds_it() -> None:
    registry = registry_for((("gw.test", "anthropic"),))
    assert [p.name for p in registry.candidates("gw.test")] == ["anthropic"]
    assert registry.is_built_in_host("api.anthropic.com")


# The tools a request offers.


def offered(**fields: Any) -> Any:
    return built_in().get("anthropic").offered("/v1/messages", wrap_fields({**BODY, **fields}))


def test_tools_custom_and_untyped_are_agent_tools_by_name() -> None:
    found = offered(
        tools=[
            {"name": "get_order", "input_schema": {}},
            {"type": "custom", "name": "get_stock", "input_schema": {}},
        ]
    )
    assert found.agent == ["get_order", "get_stock"]
    assert not (found.hosted_reads or found.hosted_writes or found.unknown)


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("bash_20250124", "bash"),
        ("text_editor_20250728", "str_replace_based_edit_tool"),
        ("memory_20250818", "memory"),
        ("computer_20250124", "computer"),
    ],
)
def test_tools_the_agent_runs_for_the_model_are_agent_tools_by_their_fixed_name(
    kind: str, name: str
) -> None:
    found = offered(tools=[{"type": kind, "name": name}])
    assert found.agent == [name]
    assert not (found.hosted_reads or found.hosted_writes or found.unknown)


def test_tools_a_built_in_with_no_name_is_named_by_its_family() -> None:
    assert offered(tools=[{"type": "bash_20250124"}]).agent == ["bash"]


@pytest.mark.parametrize(
    "kind",
    [
        "web_search_20250305",
        "web_fetch_20250910",
        "code_execution_20250825",
        "tool_search_tool_regex_20251119",
        "tool_search_tool_bm25",
    ],
)
def test_tools_run_at_anthropic_that_only_read_are_hosted_reads(kind: str) -> None:
    found = offered(tools=[{"type": kind, "name": "x"}])
    assert len(found.hosted_reads) == 1
    assert not (found.agent or found.hosted_writes or found.unknown)


def test_tools_an_mcp_server_is_a_hosted_write_named_for_each_server() -> None:
    servers = [
        {"type": "url", "url": "https://a.example/mcp", "name": "docs"},
        {"type": "url", "url": "https://b.example/mcp", "name": "orders"},
    ]
    found = offered(mcp_servers=servers)
    assert found.hosted_writes == ["docs", "orders"]
    both = offered(mcp_servers=servers, tools=[{"type": "mcp_toolset", "mcp_server_name": "docs"}])
    assert both.hosted_writes == ["docs", "orders"], "a toolset names a server once"
    assert not both.unknown


@pytest.mark.parametrize(
    "kind", ["browser_toolset_20260101", "computer_toolset_20260101", "made_up_tool"]
)
def test_tools_a_type_that_is_not_known_is_unknown_and_so_refused(kind: str) -> None:
    found = offered(tools=[{"type": kind, "name": "x"}])
    assert found.unknown == [kind]
    assert not (found.agent or found.hosted_reads or found.hosted_writes)


def test_tools_a_request_with_none_or_junk_offers_nothing() -> None:
    for fields in ({}, {"tools": None}, {"tools": "x"}, {"tools": [7, None, "x"]}, {"tools": {}}):
        found = offered(**fields)
        assert not found.hosted_reads and not found.hosted_writes


def run_with_tools(tools: list[dict[str, Any]], real: tuple[str, ...] = (), **fields: Any) -> Gate:
    run = Gate(hosts=(), prices=(("claude-test", Rates(3.0, 15.0)),), real=real)
    run.run.listed.add("get_order")
    provider = Provider(run.events, answered)
    client = httpx2.Client(transport=httpx2.MockTransport(provider))
    client.post(URL, json={**BODY, "tools": tools, **fields})
    return run


def test_tools_an_agent_tool_a_gate_sees_goes_through() -> None:
    run = run_with_tools([{"name": "get_order", "input_schema": {}}])
    assert run.line["status"] == 200


def test_tools_an_agent_tool_no_gate_sees_is_refused_as_unseen() -> None:
    with pytest.raises(ScrimmageRefused, match="get_other"):
        run_with_tools([{"name": "get_other", "input_schema": {}}])


def test_tools_web_search_is_a_hosted_read_and_goes_through() -> None:
    run = run_with_tools([{"type": "web_search_20250305", "name": "web_search"}])
    assert run.line["status"] == 200


def test_tools_an_mcp_server_needs_hosted_mcp_under_real() -> None:
    servers = [{"type": "url", "url": "https://a.example/mcp", "name": "docs"}]
    with pytest.raises(ScrimmageRefused, match=r"hosted:mcp/docs"):
        run_with_tools([], mcp_servers=servers)
    run = run_with_tools([], real=("hosted:mcp/docs",), mcp_servers=servers)
    assert run.line["status"] == 200


def test_tools_a_browser_toolset_is_refused_as_an_unknown_type() -> None:
    with pytest.raises(ScrimmageRefused, match="browser_toolset"):
        run_with_tools([{"type": "browser_toolset_20260101", "name": "browser"}])
