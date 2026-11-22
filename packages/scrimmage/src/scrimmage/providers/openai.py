"""OpenAI as a ModelProvider: each method calls the matching function of provider_openai.

The calls go through the module, by name, when they are made, so the module stays the one
place OpenAI's forms are written."""

from collections.abc import Sequence

from scrimmage import provider_openai as table
from scrimmage.prices import Rates
from scrimmage.providers.base import CHAT, RESPONSES, Given, Offered, Usage


class OpenAI:
    name = "openai"

    @property
    def hosts(self) -> tuple[str, ...]:
        return table.HOSTS

    @property
    def apis(self) -> tuple[str, ...]:
        return (CHAT, RESPONSES)

    @property
    def free_ops(self) -> tuple[str, ...]:
        return table.FREE_OPS

    # Recognizer

    def op_for(self, path: str, method: str) -> str | None:
        return table.op_for(path, method)

    def api(self, path: str) -> str:
        return RESPONSES if table.is_responses(path) else CHAT

    def request_fields(self, body: bytes) -> dict[str, object] | None:
        return table.request_fields(body)

    def model_name(self, fields: dict[str, object] | None) -> str | None:
        return table.model_name(fields)

    def offered(self, path: str, fields: dict[str, object]) -> Offered:
        return table.offered(path, fields)

    # Meter

    def usage(self, body: bytes, *, output_required: bool = True) -> Usage:
        return table.usage(body, output_required=output_required)

    def max_output(self, fields: dict[str, object]) -> int | None:
        return table.max_output(fields)

    def choices(self, fields: dict[str, object]) -> int:
        return table.choices(fields)

    def request_tier(self, fields: dict[str, object] | None) -> str | None:
        return table.request_tier(fields)

    def gateway_cost(self, value: object, before: float | None = None) -> float | None:
        return table.gateway_cost(value, before)

    def price(self, model: str, overrides: dict[str, Rates]) -> Rates | None:
        return table.price(model, overrides)

    def cost(
        self, tokens_in: int, tokens_out: int, rates: Rates, cached: int = 0, *writes: int
    ) -> float:
        return table.cost(tokens_in, tokens_out, rates, cached, *writes)

    def dearest(
        self, rates: Rates, tokens_in: int, tokens_out: int, cached: int, *writes: int
    ) -> Rates:
        return table.dearest(rates, tokens_in, tokens_out, cached, *writes)

    def most_cost(self, tokens_in: int, tokens_out: int, rates: Rates, *, path: str) -> float:
        return table.most_cost(tokens_in, tokens_out, rates, responses=table.is_responses(path))

    # FaultMaker

    def error_body(self, status: int) -> bytes:
        return table.error_body(status)

    def error_type(self, status: int) -> str:
        return table.error_type(status)

    def broken_body(self, path: str, op: str) -> bytes:
        return table.broken_body(path, op)

    def context_too_long_body(self) -> bytes:
        return table.context_too_long_body()

    def refusal_reply(self, path: str, fields: dict[str, object], tokens_in: int) -> bytes:
        if table.is_responses(path):
            return table.response_refusal_reply(fields, tokens_in)
        return table.refusal_reply(fields, tokens_in)

    def tool_call_tools(self, path: str, fields: dict[str, object]) -> list[str]:
        return table.tool_call_tools(path, fields)

    def no_free_tool_call(self, path: str, fields: dict[str, object]) -> str | None:
        return table.no_free_tool_call(path, fields)

    def unknown_tool_reply(self, fields: dict[str, object], tokens_in: int, tool: str) -> bytes:
        return table.unknown_tool_reply(fields, tokens_in, tool)

    def given(self, fields: dict[str, object], reply: bytes, *, ids_only: bool) -> Given:
        return table.given(fields, reply, ids_only=ids_only)

    def carries(self, fields: dict[str, object], kept: Sequence[Given]) -> bool:
        return table.carries(fields, kept)

    def references(self, fields: dict[str, object]) -> list[str]:
        return table.references(fields)

    def response_ids(self, reply: bytes) -> list[str]:
        return table.response_ids(reply)

    # ReplyChanger

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
