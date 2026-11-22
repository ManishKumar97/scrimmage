"""One contract every model provider keeps, run over each provider the registry holds.

A provider needs a Case here before it can be registered, so a new provider is held to the
same checks as OpenAI: it reads usage, builds error bodies, names ops, and never prices a
call below its table."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scrimmage.prices import Rates
from scrimmage.providers.base import ModelProvider
from scrimmage.providers.registry import PROVIDERS

OPENAI = Path(__file__).parent / "fixtures" / "openai"
STATUSES = (400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504, 529)
RATES = Rates(2.0, 8.0, 0.5, 3.0, 4.0)
JUNK = (b"", b"{", b"[]", b"null", b'"text"', b"data: not json\n\ndata: [DONE]\n", b"\xff\xfe")


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
    )
}


@pytest.fixture(params=PROVIDERS, ids=lambda p: p.name)
def provider(request: pytest.FixtureRequest) -> ModelProvider:
    return request.param


def case_of(provider: ModelProvider) -> Case:
    assert provider.name in CASES, f"{provider.name} is registered with no contract case"
    return CASES[provider.name]


def test_every_case_is_for_a_registered_provider() -> None:
    assert set(CASES) == {p.name for p in PROVIDERS}


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
    assert "/" in provider.error_type(status), "a content type"


def test_a_broken_body_does_not_parse(provider: ModelProvider) -> None:
    case = case_of(provider)
    for op in ("chat", "embeddings"):
        broken = provider.broken_body(case.chat_path, op)
        assert broken
        with pytest.raises(ValueError):
            json.loads(broken)


def test_context_too_long_is_a_json_error(provider: ModelProvider) -> None:
    body = json.loads(provider.context_too_long_body())
    assert body["error"]["message"]


def test_the_api_of_a_path_is_one_the_provider_lists(provider: ModelProvider) -> None:
    for _, path, op in case_of(provider).ops:
        if op:
            assert provider.api(path.split("?")[0]) in provider.apis


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
    usd = provider.cost(tokens_in, tokens_out, RATES, cached, *writes)
    # The cheapest any input token can cost is the cached rate, and each output token costs the
    # output rate: no breakdown of the tokens prices the call under that.
    floor = (tokens_in * (RATES.cached_input or 0.0) + tokens_out * RATES.output) / 1_000_000
    assert usd >= floor - 1e-12
    if not cached and not any(writes):
        assert usd == pytest.approx((tokens_in * RATES.input + tokens_out * RATES.output) / 1e6)
    # The hold is the most the call can cost on the API its path names, so on some API of the
    # provider it is never under what the call settles at.
    held = max(
        provider.most_cost(tokens_in, tokens_out, RATES, path=path)
        for path in (case.chat_path, *case.other_paths)
    )
    assert held >= usd - 1e-12


def test_the_dearest_rates_are_the_ones_the_call_costs_most_at(provider: ModelProvider) -> None:
    tier = Rates(4.0, 16.0)
    rates = Rates(2.0, 8.0, tiers=(("priority", tier),))
    assert provider.dearest(rates, 100, 100, 0, 0, 0) == tier
