"""One contract every model provider keeps, run over each provider the registry holds.

A provider needs a Case here before it can be registered, so a new provider is held to the
same checks as OpenAI: it reads requests and usage, builds error bodies and replies, names ops,
changes a real reply, says what a faulted reply gave the agent, and never prices a call below
its table. A provider that keeps its replies on its own side also keeps the chain contract."""

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scrimmage import changes
from scrimmage.config import Fault
from scrimmage.prices import RATE_KEYS, Rates
from scrimmage.providers.base import ModelProvider, Reply, ServerChains
from scrimmage.providers.impl.wire import fields_dict, wrap_fields
from scrimmage.providers.registry import builtin_providers

OPENAI = Path(__file__).parent / "fixtures" / "openai"
STATUSES = (400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504, 529)
RATES = Rates(2.0, 8.0, 0.5, 3.0, 4.0)
JUNK = (b"", b"{", b"[]", b"null", b'"text"', b"data: not json\n\ndata: [DONE]\n", b"\xff\xfe")
JSON = [("content-type", "application/json")]
REPLY_WORDS = ("replace", "garble", "truncate")
EVERY_WORD = (*REPLY_WORDS, "unknown tool", "refuse", "bad tool args", "context too long")


@dataclass(frozen=True)
class Chain:
    """What a provider that keeps its replies gives: the path of that API, a saved reply to it,
    the id the reply holds, and a request that names that id."""

    path: str
    reply: Path
    reply_id: str
    names_it: Callable[[str], dict[str, object]]


@dataclass(frozen=True)
class Case:
    host: str
    # (method, path, op): what op_for says of this request.
    ops: tuple[tuple[str, str, str | None], ...]
    # (fixture, input, output, cached): the counts usage reads from a saved reply.
    usages: tuple[tuple[Path, int, int, int], ...]
    chat_path: str
    other_paths: tuple[str, ...]  # the provider's other model APIs
    shipped_model: str  # a model the shipped price table has
    write_fields: tuple[str, ...]  # the Rates fields that price a cache write
    request: dict[str, object]  # a request the provider reads, with its model
    model: str
    tool_request: Callable[[str], dict[str, object]]  # a request that offers one agent tool
    text_request: Callable[[str], dict[str, object]]  # a request whose input holds this text
    follow_up: Callable[[dict[str, object], bytes], dict[str, object]]  # the agent's next request
    changeable: Path  # a saved reply the three changes can change
    # (word, path, fields) that cannot be carried out: a mistake in the test.
    not_carried: tuple[tuple[str, str, dict[str, object]], ...]
    capped: Callable[[int], dict[str, object]]  # a request that caps its output at this many
    # A request that asks for this many replies, each capped at this many tokens; None when the
    # provider's API gives one reply for each request.
    replies: Callable[[int, int], dict[str, object]] | None = None
    # The keys of this provider's requests and replies that name its shape, which no module
    # outside the provider may decide by.
    wire_keys: tuple[str, ...] = ()
    # A saved reply that calls a tool, the tool's name, and the parts of replies as this
    # provider's changes record them: the text of the changeable reply, and the first call's
    # arguments of the tool call reply.
    call_reply: Path | None = None
    call_name: str = ""
    text_part: str = ""
    arguments_part: str = ""
    chain: Chain | None = None


def openai_tool_request(name: str) -> dict[str, object]:
    function = {"name": name, "parameters": {"type": "object", "properties": {}}}
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": function}],
    }


def openai_follow_up(fields: dict[str, object], reply: bytes) -> dict[str, object]:
    """The next request an agent builds from a reply: its message, then each tool's result."""
    message = json.loads(reply)["choices"][0]["message"]
    results = [
        {"role": "tool", "tool_call_id": call["id"], "content": "ok"}
        for call in message.get("tool_calls") or []
    ]
    asked: list[Any] = list(fields["messages"])  # type: ignore[arg-type]
    return {**fields, "messages": [*asked, message, *results]}


CASES = {
    "openai": Case(
        host="api.openai.com",
        ops=(
            ("POST", "/v1/chat/completions", "chat"),
            ("POST", "/v1/chat/completions/", "chat"),
            ("POST", "/v1/chat/completions?api-version=1", "chat"),
            ("POST", "/openai/v1/responses", "chat"),
            ("POST", "/v1/embeddings", "embeddings"),
            ("POST", "/v1/moderations", "moderations"),
            ("POST", "/v1/responses/input_tokens", "input_tokens"),
            ("GET", "/v1/chat/completions", None),
            ("POST", "/v1/models", None),
            ("POST", "/", None),
            ("POST", "", None),
        ),
        usages=(
            (OPENAI / "chat_answer.json", 19, 10, 0),
            (OPENAI / "chat_answer_cached.json", 2048, 10, 1536),
            (OPENAI / "real_chat_answer.json", 158, 18, 0),
            (OPENAI / "response_text.json", 36, 87, 0),
            (OPENAI / "chat_stream_usage.sse", 9, 1, 0),
            (OPENAI / "chat_stream_usage_cached.sse", 2048, 1, 2048),
        ),
        chat_path="/v1/chat/completions",
        other_paths=("/v1/responses",),
        shipped_model="gpt-4o-mini",
        write_fields=("cache_write_30m", "cache_write_12h"),
        request={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        model="gpt-4o-mini",
        tool_request=openai_tool_request,
        text_request=lambda text: {
            "model": "gpt-4o-mini",
            "messages": [{"role": "tool", "content": f"note: {text}"}],
        },
        follow_up=openai_follow_up,
        changeable=OPENAI / "chat_answer.json",
        not_carried=(
            ("unknown tool", "/v1/responses", {"model": "m", "input": "hi"}),
            ("garble", "/v1/responses", {"model": "m", "conversation": "c"}),
            ("context too long", "/v1/responses", {"model": "m", "input": "hi"}),
        ),
        capped=lambda n: {"model": "gpt-4o-mini", "messages": [], "max_tokens": n},
        call_reply=OPENAI / "real_chat_tool_call.json",
        call_name="get_order",
        text_part="choices[0].message.content",
        arguments_part="choices[0].message.tool_calls[0].function.arguments",
        wire_keys=(
            "choices",
            "tool_calls",
            "finish_reason",
            "previous_response_id",
            "conversation",
            "item_reference",
            "max_completion_tokens",
            "max_output_tokens",
        ),
        replies=lambda n, cap: {"model": "gpt-4o-mini", "messages": [], "n": n, "max_tokens": cap},
        chain=Chain(
            path="/v1/responses",
            reply=OPENAI / "response_text.json",
            reply_id="resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3795b",
            names_it=lambda found: {"model": "m", "previous_response_id": found},
        ),
    )
}


@pytest.fixture(params=builtin_providers(), ids=lambda p: p.name)
def provider(request: pytest.FixtureRequest) -> ModelProvider:
    return request.param


def case_of(provider: ModelProvider) -> Case:
    assert provider.name in CASES, f"{provider.name} is registered with no contract case"
    return CASES[provider.name]


def body_of(fields: dict[str, object]) -> bytes:
    return json.dumps(fields).encode()


def counts(provider: ModelProvider, body: bytes) -> tuple[bool, int, int, int]:
    used = provider.usage(body)
    return used.known, used.input, used.output, used.cached


def test_every_case_is_for_a_registered_provider() -> None:
    assert set(CASES) == {p.name for p in builtin_providers()}


def test_a_provider_names_itself_and_its_hosts(provider: ModelProvider) -> None:
    assert provider.name
    assert provider.name == provider.name.lower()
    assert case_of(provider).host in provider.hosts
    assert all(host == host.lower() and "/" not in host for host in provider.hosts)


def test_op_for_knows_the_paths_of_its_calls(provider: ModelProvider) -> None:
    for method, path, op in case_of(provider).ops:
        assert provider.op_for(path, method) == op, f"{method} {path}"


def test_free_ops_are_ops_the_provider_names(provider: ModelProvider) -> None:
    named = {op for _, _, op in case_of(provider).ops if op}
    assert set(provider.free_ops) <= named


def test_a_request_is_read_as_the_object_it_is_and_nothing_else(provider: ModelProvider) -> None:
    case = case_of(provider)
    assert fields_dict(provider.request_fields(body_of(case.request))) == case.request
    for body in JUNK:
        assert provider.request_fields(body) is None, body


def test_the_model_is_the_name_a_request_gives_and_none_when_it_gives_none(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    assert provider.model_name(wrap_fields(case.request)) == case.model
    nameless: list[dict[str, object] | None] = [
        None,
        {},
        {"model": ""},
        {"model": 7},
        {"model": None},
        {"model": ["m"]},
    ]
    for fields in nameless:
        assert provider.model_name(None if fields is None else wrap_fields(fields)) is None, fields


def test_the_tools_a_request_offers_are_sorted(provider: ModelProvider) -> None:
    case = case_of(provider)
    offered = provider.offered(case.chat_path, wrap_fields(case.tool_request("get_order")))
    assert offered.agent == ["get_order"]
    assert not (offered.hosted_reads or offered.hosted_writes or offered.unknown)
    for fields in ({}, case.request):
        nothing = provider.offered(case.chat_path, wrap_fields(fields))
        assert not (nothing.agent or nothing.hosted_reads or nothing.hosted_writes)
        assert not nothing.unknown


def test_usage_reads_a_saved_reply(provider: ModelProvider) -> None:
    for fixture, tokens_in, tokens_out, cached in case_of(provider).usages:
        used = provider.usage(fixture.read_bytes())
        found = (used.known, used.input, used.output, used.cached)
        assert found == (True, tokens_in, tokens_out, cached), fixture.name
        assert 0 <= used.cached <= used.input


def test_usage_of_a_body_with_no_counts_is_not_known(provider: ModelProvider) -> None:
    for body in JUNK:
        used = provider.usage(body)
        assert not used.known, body
        assert (used.input, used.output) == (0, 0)


def test_the_cache_writes_a_reply_makes_are_counts_by_a_price_field(
    provider: ModelProvider,
) -> None:
    """usage names each write count by the Rates field that prices it, and no other."""
    case = case_of(provider)
    for fixture, *_ in case.usages:
        used = provider.usage(fixture.read_bytes())
        assert set(used.writes) <= set(case.write_fields), fixture.name
        assert set(case.write_fields) <= RATE_KEYS
        assert all(isinstance(n, int) and n >= 0 for n in used.writes.values())
        assert sum(used.writes.values()) <= used.input


@pytest.mark.parametrize("status", STATUSES)
def test_an_error_body_is_the_providers_error_for_each_status(
    provider: ModelProvider, status: int
) -> None:
    body = json.loads(provider.error_body(status))
    assert isinstance(body, dict)
    error = body["error"]
    assert isinstance(error, dict)
    assert isinstance(error["message"], str)
    assert error["message"]
    assert "/" in provider.error_content_type(status), "a content type"


def test_a_broken_body_does_not_parse(provider: ModelProvider) -> None:
    case = case_of(provider)
    for op in ("chat", "embeddings"):
        broken = provider.broken_body(case.chat_path, op)
        assert broken
        with pytest.raises(ValueError):
            json.loads(broken)


@pytest.mark.parametrize("kind", ["application/json", "text/plain"])
def test_the_headers_of_a_fault_reply_hold_the_type_and_a_request_id(
    provider: ModelProvider, kind: str
) -> None:
    headers = provider.fault_headers(kind)
    assert headers["content-type"] == kind
    assert len(headers) >= 2, "a request id header besides the type"
    assert all(k == k.lower() and isinstance(v, str) and v for k, v in headers.items())
    assert "retry-after" not in headers, "the gate adds the delay of a status fault"
    assert provider.fault_headers(kind) is not provider.fault_headers(kind)


def test_the_hold_may_read_the_request_and_never_costs_less_than_the_settle(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    for fields in ({}, case.request, case.capped(50)):
        held = provider.most_cost(1000, 500, RATES, path=case.chat_path, fields=wrap_fields(fields))
        assert held >= provider.cost(1000, 500, RATES, 0, {}) - 1e-12


def test_context_too_long_is_a_json_error(provider: ModelProvider) -> None:
    body = json.loads(provider.context_too_long_body())
    assert body["error"]["message"]


def test_a_built_refusal_is_a_whole_reply_the_provider_would_read(provider: ModelProvider) -> None:
    case = case_of(provider)
    for path in (case.chat_path, *case.other_paths):
        built = provider.refusal_reply(path, wrap_fields(case.request), 25)
        assert isinstance(json.loads(built), dict), path
        used = provider.usage(built)
        assert used.known, path
        assert (used.input, used.cached) == (25, 0), path
        assert used.output > 0, path


def test_a_built_call_to_an_unknown_tool_names_it_and_is_a_whole_reply(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    fields = case.tool_request("get_order")
    assert provider.tool_call_tools(case.chat_path, wrap_fields(fields)) == ["get_order"]
    assert provider.no_free_tool_call(case.chat_path, wrap_fields(fields)) is None
    built = provider.unknown_tool_reply(wrap_fields(fields), 25, "get_order")
    assert isinstance(json.loads(built), dict)
    assert "get_order" in built.decode()
    used = provider.usage(built)
    assert (used.known, used.input) == (True, 25)


def test_a_request_with_no_tool_to_call_says_why(provider: ModelProvider) -> None:
    case = case_of(provider)
    assert provider.tool_call_tools(case.chat_path, wrap_fields(case.request)) == []
    why = provider.no_free_tool_call(case.chat_path, wrap_fields(case.request))
    assert isinstance(why, str) and why


def test_a_fault_that_cannot_be_carried_out_says_why_and_the_rest_do_not(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    for word in EVERY_WORD:
        assert provider.not_carried_out(word, case.chat_path, wrap_fields(case.request)) is None, (
            word
        )
    for word, path, fields in case.not_carried:
        why = provider.not_carried_out(word, path, wrap_fields(fields))
        assert isinstance(why, str) and why, (word, path)


def test_what_a_faulted_reply_gave_is_carried_back_by_the_request_built_from_it(
    provider: ModelProvider,
) -> None:
    """The round trip: a faulted reply, the request an agent builds from it, and a request it
    did not build."""
    case = case_of(provider)
    fields = case.tool_request("get_order")
    reply = provider.unknown_tool_reply(wrap_fields(fields), 25, "get_order")
    for ids_only in (True, False):
        gave = provider.given(case.chat_path, wrap_fields(fields), reply, ids_only=ids_only)
        assert gave.ids, "a reply that calls a tool gave its call id"
        built = case.follow_up(fields, reply)
        assert provider.carries(case.chat_path, wrap_fields(built), [gave]), ids_only
        assert not provider.carries(case.chat_path, wrap_fields(fields), [gave]), ids_only
        assert not provider.carries(case.chat_path, wrap_fields(built), []), ids_only
    refused = provider.refusal_reply(case.chat_path, wrap_fields(case.request), 25)
    gave = provider.given(case.chat_path, wrap_fields(case.request), refused, ids_only=False)
    assert gave.prefixes, "a reply with a message gave its message"
    assert provider.carries(
        case.chat_path, wrap_fields(case.follow_up(case.request, refused)), [gave]
    )
    assert not provider.carries(case.chat_path, wrap_fields(case.request), [gave])


def test_a_provider_that_keeps_its_replies_keeps_the_chain_contract(
    provider: ModelProvider,
) -> None:
    """ServerChains is optional: a provider has it exactly when its case describes a chain."""
    case = case_of(provider)
    assert isinstance(provider, ServerChains) == (case.chain is not None)
    if case.chain is None or not isinstance(provider, ServerChains):
        return
    chain = case.chain
    ids = provider.chain_ids(chain.path, chain.reply.read_bytes())
    assert chain.reply_id in ids
    assert provider.chain_ids(chain.path, b"{") == []
    assert provider.chain_ids(case.chat_path, chain.reply.read_bytes()) == []
    named = provider.chain_references(chain.path, wrap_fields(chain.names_it(chain.reply_id)))
    assert named == [chain.reply_id]
    assert (
        provider.chain_references(case.chat_path, wrap_fields(chain.names_it(chain.reply_id))) == []
    )
    assert provider.chain_references(chain.path, wrap_fields(case.request)) == []
    assert chain.reply_id in provider.chain_broken(chain.reply_id)
    gave = provider.given(
        chain.path, wrap_fields(case.request), chain.reply.read_bytes(), ids_only=False
    )
    assert not (gave.ids or gave.prefixes), "a reply kept on the provider's side gives no markers"
    assert not provider.carries(chain.path, wrap_fields(case.request), [gave])


def test_a_provider_with_no_chain_has_none_of_it(provider: ModelProvider) -> None:
    if case_of(provider).chain is None:
        assert not hasattr(provider, "chain_ids")


@pytest.mark.parametrize("word", REPLY_WORDS)
def test_a_real_reply_is_changed_and_keeps_what_it_billed(
    provider: ModelProvider, word: str
) -> None:
    case = case_of(provider)
    real = case.changeable.read_bytes()
    fault = Fault("model", word, "always", text="NEW" if word == "replace" else None)
    change = provider.change(word, "chat", case.chat_path)
    assert change.blocked(200, JSON) is None
    done = change(200, JSON, real, fault, decoded=False)
    assert isinstance(done, changes.WebChanged), done
    assert done.body != real
    assert isinstance(json.loads(done.body), dict)
    assert counts(provider, done.body) == counts(provider, real), "the change left usage alone"
    assert ("NEW" in done.body.decode()) == (word == "replace")
    assert dict(done.headers)["content-length"] == str(len(done.body))


@pytest.mark.parametrize("word", REPLY_WORDS)
def test_a_reply_that_cannot_take_the_change_says_why(provider: ModelProvider, word: str) -> None:
    case = case_of(provider)
    change = provider.change(word, "chat", case.chat_path)
    stream = [("content-type", "text/event-stream")]
    packed = [*JSON, ("content-encoding", "zstd")]
    for status, headers in ((500, JSON), (200, stream), (200, packed), (200, [])):
        said = change.blocked(status, headers)
        assert isinstance(said, changes.Unchangeable), (status, headers)
        assert said.why
    fault = Fault("model", word, "always", text="NEW" if word == "replace" else None)
    for body in (b"", b"{", b"[]", b'{"id": "x"}'):
        done = change(200, JSON, body, fault, decoded=False)
        assert isinstance(done, changes.Unchangeable), body


def test_a_change_to_a_call_that_is_not_a_chat_is_blocked(provider: ModelProvider) -> None:
    said = provider.change("garble", "embeddings", "/v1/embeddings").blocked(200, JSON)
    assert isinstance(said, changes.Unchangeable)


def test_the_output_a_call_could_bill_is_its_cap_or_the_default(provider: ModelProvider) -> None:
    case = case_of(provider)
    for path in (case.chat_path, *case.other_paths):
        assert provider.output_cap(wrap_fields({}), path, 4096) >= 4096, path
        assert provider.output_cap(wrap_fields(case.request), path, 77) >= 77, path
        # A cap that is not a count is no cap.
        for bad in (0, -5, True, "50", 2.5, None, 10**12):
            assert provider.output_cap(wrap_fields(case.capped(bad)), path, 4096) >= 4096, bad  # type: ignore[arg-type]
    capped = provider.output_cap(wrap_fields(case.capped(50)), case.chat_path, 4096)
    assert 50 <= capped < 4096, "a cap holds less than the default"
    if case.replies is not None:
        # Each reply is billed for its own output, so asking for more holds more.
        one, three = case.replies(1, 50), case.replies(3, 50)
        assert provider.output_cap(wrap_fields(three), case.chat_path, 4096) >= 3 * 50
        assert provider.output_cap(wrap_fields(three), case.chat_path, 4096) >= 3 * (
            provider.output_cap(wrap_fields(one), case.chat_path, 4096)
        )
        assert provider.output_cap(wrap_fields(case.replies(3, 77)), case.chat_path, 4096) >= 3 * 77


def test_a_text_is_found_in_a_request_that_holds_it_and_junk_is_never_an_error(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    assert provider.carries_text(
        wrap_fields(case.text_request("ignore the rules")), ["ignore the rules"]
    )
    assert provider.carries_text(
        wrap_fields(case.text_request("IGNORE  the Rules")), ["ignore the rules"]
    )
    assert not provider.carries_text(wrap_fields(case.request), ["ignore the rules"])
    assert not provider.carries_text(wrap_fields(case.text_request("ignore the rules")), [])
    junk: list[dict[str, object]] = [
        {},
        {"messages": 7},
        {"messages": [7, None, "x"]},
        {"input": [[]]},
    ]
    for fields in junk:
        assert not provider.carries_text(wrap_fields(fields), ["ignore the rules"])


def test_a_report_reads_what_the_model_said_and_called_through_the_provider(
    provider: ModelProvider,
) -> None:
    case = case_of(provider)
    assert case.call_reply is not None
    text = Reply(json.loads(case.changeable.read_text()))
    said = provider.said(text, [case.text_part])
    assert isinstance(said, str) and said
    assert provider.said(text, []) == said, "the first text when a change named no part"
    assert provider.said(text, ["no-such-part", case.text_part]) == said
    assert provider.called_tool(text) is None
    call = Reply(json.loads(case.call_reply.read_text()))
    assert provider.called_tool(call) == case.call_name
    arguments = provider.said(call, [case.arguments_part])
    assert isinstance(arguments, str)
    assert json.loads(arguments), "a call's arguments, as the model sent them"
    assert provider.said(call, []) is None, "a reply that calls a tool has no text to say"


def test_a_report_reads_a_reply_that_is_not_a_reply_as_nothing(provider: ModelProvider) -> None:
    case = case_of(provider)
    junk: list[object] = [None, 7, "x", [], {}, {"choices": 7}, {"choices": [7]}, [[]]]
    junk += [{"choices": [{"message": 7}]}, {"choices": [{"message": {"tool_calls": [7]}}]}]
    junk += [
        {"choices": [{"message": {"content": 7}}]},
        {"choices": [{"message": {"content": ["x"]}}]},
    ]
    for reply in junk:
        assert provider.called_tool(Reply(reply)) is None, reply
        assert provider.said(Reply(reply), []) is None, reply
        assert provider.said(Reply(reply), [case.text_part, case.arguments_part]) is None, reply


def test_a_gateway_cost_is_a_finite_amount_and_nothing_else(provider: ModelProvider) -> None:
    for good, said in ((0.25, 0.25), (0, 0.0), ("0.5", 0.5)):
        assert provider.gateway_cost(good) == said, good
    for junk in (None, True, "abc", "nan", -1, float("inf"), "-2", [], {}):
        assert provider.gateway_cost(junk) is None, junk
        assert provider.gateway_cost(junk, 0.75) == 0.75, junk


def test_the_tier_a_request_names_is_none_when_it_names_none(provider: ModelProvider) -> None:
    case = case_of(provider)
    for fields in (None, {}, case.request):
        assert provider.request_tier(None if fields is None else wrap_fields(fields)) is None


def test_a_price_is_the_shipped_row_and_a_team_row_wins(provider: ModelProvider) -> None:
    model = case_of(provider).shipped_model
    assert provider.price(model, {}) is not None
    assert provider.price(model, {model: RATES}) == RATES
    assert provider.price("no-such-model-anywhere", {}) is None


@pytest.mark.parametrize(
    ("tokens_in", "tokens_out", "cached", "writes"),
    [
        (0, 0, 0, (0, 0)),
        (1000, 0, 0, (0, 0)),
        (0, 500, 0, (0, 0)),
        (1000, 500, 0, (0, 0)),
        (1000, 500, 400, (0, 0)),
        (1000, 500, 0, (300, 0)),
        (1000, 500, 100, (200, 300)),
        (1000, 500, 0, (0, 1000)),
    ],
)
def test_a_cost_is_never_below_the_table(
    provider: ModelProvider, tokens_in: int, tokens_out: int, cached: int, writes: tuple[int, int]
) -> None:
    case = case_of(provider)
    counted = dict(zip(case.write_fields, writes, strict=False))
    usd = provider.cost(tokens_in, tokens_out, RATES, cached, counted)
    # The cheapest any input token can cost is the cached rate, and each output token costs the
    # output rate: no breakdown of the tokens prices the call under that.
    floor = (tokens_in * (RATES.cached_input or 0.0) + tokens_out * RATES.output) / 1_000_000
    assert usd >= floor - 1e-12
    if not cached and not any(writes):
        assert usd == pytest.approx((tokens_in * RATES.input + tokens_out * RATES.output) / 1e6)
        assert provider.cost(tokens_in, tokens_out, RATES) == pytest.approx(usd)
    # The hold is the most the call can cost on the API its path names, so on some API of the
    # provider it is never under what the call settles at.
    held = max(
        provider.most_cost(
            tokens_in, tokens_out, RATES, path=path, fields=wrap_fields(case.request)
        )
        for path in (case.chat_path, *case.other_paths)
    )
    assert held >= usd - 1e-12


def test_a_write_is_priced_at_its_own_rate(provider: ModelProvider) -> None:
    """A write count moves the cost by the rate of the Rates field it is named by."""
    case = case_of(provider)
    free = dict.fromkeys(case.write_fields, 0.0)
    rates = dataclasses.replace(Rates(1.0, 1.0, 0.1), **free)
    base = provider.cost(1000, 0, rates, 0, {})
    for field in case.write_fields:
        dear = dataclasses.replace(rates, **{**free, field: 5.0})
        assert provider.cost(1000, 0, dear, 0, {field: 100}) == pytest.approx(base + 100 * 4 / 1e6)
        for other in set(case.write_fields) - {field}:
            assert provider.cost(1000, 0, dear, 0, {other: 100}) == pytest.approx(
                base - 100 * 1 / 1e6
            )


def test_the_dearest_rates_are_the_ones_the_call_costs_most_at(provider: ModelProvider) -> None:
    tier = Rates(4.0, 16.0)
    rates = Rates(2.0, 8.0, tiers=(("priority", tier),))
    assert provider.dearest(rates, 100, 100, 0, {}) == tier


def test_the_dearest_rates_count_the_writes_of_the_call(provider: ModelProvider) -> None:
    """A tier that writes dearer is the dearest only for a call that wrote."""
    case = case_of(provider)
    for field in case.write_fields:
        tier = dataclasses.replace(Rates(1.0, 1.0), **{field: 10.0})
        rates = Rates(1.0, 1.0, tiers=(("t", tier),))
        assert provider.dearest(rates, 100, 0, 0, {field: 100}) == tier
        assert provider.dearest(rates, 100, 0, 0, {}) == rates
