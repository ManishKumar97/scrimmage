"""OpenAI as a ModelProvider.

A class that holds OpenAI's functions from provider_openai, bound once at import, and
adapts the few whose signature the interface changes: the ones that take a path, because
OpenAI has two APIs, Chat Completions and Responses, and decides by path which one a call
is. Nothing outside this package asks which API a path is.
"""

from collections.abc import Mapping, Sequence

from scrimmage import provider_openai as table
from scrimmage.changes import Unchangeable, WebChanged
from scrimmage.config import Fault
from scrimmage.prices import Rates
from scrimmage.providers.base import Fields, Given, ReplyChange, Usage
from scrimmage.providers.impl import openai_changes

RESPONSES = "responses"  # the op the change of a Responses reply is looked up under


class OpenAIChange:
    """One change to an OpenAI reply, for one fault word on the API a path names."""

    def __init__(self, word: str, op: str, provider: "OpenAI") -> None:
        self.word, self.op = word, op
        self.provider = provider

    def blocked(self, status: int, headers: Sequence[tuple[str, str]]) -> Unchangeable | None:
        return openai_changes.chat_kind(status, self.op, headers, self.word)

    def __call__(
        self,
        status: int,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        fault: Fault,
        *,
        decoded: bool,
    ) -> WebChanged | Unchangeable:
        make = openai_changes.model_change(self.word, self.op)
        if self.op == RESPONSES and self.word in openai_changes.RESPONSES_CHANGES:
            return make(
                status, self.op, headers, body, fault, decoded=decoded, provider=self.provider
            )
        return make(status, self.op, headers, body, fault, decoded=decoded)


class OpenAI:
    name = "openai"
    display_name = "OpenAI"
    hosts = table.HOSTS
    free_ops = table.FREE_OPS

    # Recognizer
    op_for = staticmethod(table.op_for)
    request_fields = staticmethod(table.request_fields)
    model_name = staticmethod(table.model_name)
    offered = staticmethod(table.offered)
    carries_text = staticmethod(table.carries_text)
    streams = staticmethod(table.streams)

    # Meter
    usage = staticmethod(table.usage)
    output_cap = staticmethod(table.output_cap)
    request_tier = staticmethod(table.request_tier)
    gateway_cost = staticmethod(table.gateway_cost)

    @staticmethod
    def snapshot_family(model: str) -> str:
        """The table module's one rule, which its own price lookup reads too."""
        return table.snapshot_family(model)

    fees: tuple[str, ...] = ()  # OpenAI bills nothing for each request
    price_example = "{input = 0.15, output = 0.60} (dollars per million tokens)"

    def price(
        self, model: str, overrides: dict[str, Rates], fees: Mapping[str, float] | None = None
    ) -> Rates | None:
        return table.price(model, overrides)  # OpenAI charges no per-request fee

    def cost(self, rates: Rates, used: Usage) -> float:
        short, long = self.writes_of(used.writes)
        return table.cost(used.input, used.output, rates, used.cached, short, long)

    def dearest(self, rates: Rates, used: Usage) -> Rates:
        short, long = self.writes_of(used.writes)
        return table.dearest(rates, used.input, used.output, used.cached, short, long)

    @staticmethod
    def writes_of(writes: Mapping[str, int] | None) -> tuple[int, int]:
        """The 30 minute and the 12 hour counts, by the Rates fields that price them."""
        found = writes or {}
        return found.get(table.WRITE_30M, 0), found.get(table.WRITE_12H, 0)

    def most_cost(
        self,
        tokens_in: int,
        tokens_out: int,
        rates: Rates,
        *,
        path: str,
        fields: Fields,
    ) -> float:
        """Only the path matters to OpenAI: Responses alone writes the cache for 12 hours."""
        del fields
        return table.most_cost(tokens_in, tokens_out, rates, responses=table.is_responses(path))

    # FaultMaker
    error_body = staticmethod(table.error_body)
    error_content_type = staticmethod(table.error_content_type)
    broken_body = staticmethod(table.broken_body)

    @staticmethod
    def fault_headers(content_type: str) -> dict[str, str]:
        return {"content-type": content_type, "x-request-id": "req_scrimmage_injected"}

    context_too_long_body = staticmethod(table.context_too_long_body)
    not_carried_out = staticmethod(table.not_carried_out)
    tool_call_tools = staticmethod(table.tool_call_tools)
    no_free_tool_call = staticmethod(table.no_free_tool_call)
    unknown_tool_reply = staticmethod(table.unknown_tool_reply)

    def refusal_reply(self, path: str, wire: Fields, tokens_in: int) -> bytes:
        if table.is_responses(path):
            return table.response_refusal_reply(wire, tokens_in)
        return table.refusal_reply(wire, tokens_in)

    def given(self, path: str, wire: Fields, reply: bytes, *, ids_only: bool) -> Given:
        """Chat Completions replies are carried back by their tool call ids and messages;
        a Responses reply is kept by OpenAI and named by id, so it gives nothing to carry."""
        if table.is_responses(path):
            return Given()
        return table.given(wire, reply, ids_only=ids_only)

    def carries(self, path: str, wire: Fields, kept: Sequence[Given]) -> bool:
        return False if table.is_responses(path) else table.carries(wire, kept)

    # ReplyReader
    parse_reply = staticmethod(table.parse_reply)
    called_tool = staticmethod(table.called_tool)
    said = staticmethod(table.said)

    # ServerChains: Responses keeps its replies, Chat Completions keeps none.
    chain_ids = staticmethod(table.chain_ids)
    chain_references = staticmethod(table.chain_references)
    chain_broken = staticmethod(table.chain_broken)

    # ReplyChanger
    def change(self, word: str, op: str, path: str) -> ReplyChange:
        """The change of this fault word to a reply of the API the path names. op is what the
        call is; a Responses call is a chat to the gate, yet its reply has no choices."""
        return OpenAIChange(word, RESPONSES if table.is_responses(path) else op, self)

    def fresh_ids(self, value: dict[str, object], output: list[object]) -> dict[str, object]:
        """The changed Responses reply under new ids in OpenAI's forms, for the reply and each
        message and function call. OpenAI stored the real reply under the real ids, and an item
        resent with a stored id is read as stored, not as sent: under a made-up id the model
        reads what the agent got. call_id stays, as the agent pairs its function_call_output
        by it."""
        value["id"] = table.made_up_hex_id("resp_")
        for item in output:
            if not isinstance(item, dict):
                continue
            held: dict[str, object] = item  # pyright: ignore[reportUnknownVariableType]
            prefix = table.ITEM_PREFIXES.get(str(held.get("type")))
            if prefix is not None and "id" in held:
                held["id"] = table.made_up_hex_id(prefix)
        return value
