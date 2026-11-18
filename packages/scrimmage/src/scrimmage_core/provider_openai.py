"""OpenAI's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body. The gate uses
them to label a call, build a fault the openai library reads as a real error, count
tokens and sort the tools the model is offered.
"""

import json
import re
from dataclasses import dataclass, field
from typing import cast

HOSTS = ("api.openai.com",)
CHAT_PATHS = ("/chat/completions", "/responses")
# Provider calls that only read or compute: they run for real and are counted.
READ_PATHS = {
    "/embeddings": "embeddings",
    "/moderations": "moderations",
    "/responses/input_tokens": "input_tokens",
}
FREE_OPS = ("moderations", "input_tokens")  # no tokens billed, so nothing to reserve

# Every body is the spec's Error object. The 429 message is as OpenAI's rate-limit
# cookbook shows it, its code from the spec's error code list; the others are from
# OpenAI's error codes page. A status with no documented message gets a plain one.
MESSAGES = {
    401: "Incorrect API key provided.",
    429: (
        "Rate limit reached for default-codex in organization org-{id} on requests per "
        "min. Limit: 20.000000 / min. Current: 24.000000 / min. Contact support@openai.com "
        "if you continue to have issues or if you’d like to request an increase."  # noqa: RUF001  (OpenAI's own text)
    ),
    500: "The server had an error while processing your request. Sorry about that!",
    503: "The engine is currently overloaded, please try again later.",
}
# What bad body sends a model call: the first 60 bytes of the spec's example reply for its
# operation, so a reader that logs the body sees a real reply's start, and it cannot parse.
BROKEN_BODY = b'{\n  "id": "chatcmpl-B9MBs8CjcvOU2jLn4n570S5qMJKcT",\n  "objec'
BROKEN_EMBEDDING = b'{\n  "object": "list",\n  "data": [\n    {\n      "object": "emb'
BROKEN_RESPONSE = b'{\n  "id": "resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3'
TYPES = {429: "requests"}
CODES = {429: "rate_limit_exceeded", 401: "invalid_api_key"}


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens. With no cached rate, cached tokens cost the full input
    rate: a price that guesses must guess high, or the budget could let through more."""

    input: float
    output: float
    cached_input: float | None = None


# Checked against OpenAI's pricing page on 7 November 2026, standard tier; check again
# before each release. A team overrides or adds a model under [agent.prices].
PRICES: dict[str, Rates] = {
    "gpt-4o": Rates(2.50, 10.00, 1.25),
    # Its own price, not its family's; shut down, so no longer on the pricing page. No
    # cached rate: it had no prompt cache discount. A guess high is safe, low is not.
    "gpt-4o-2024-05-13": Rates(5.00, 15.00),
    "gpt-4o-mini": Rates(0.15, 0.60, 0.075),
    "gpt-4.1": Rates(2.00, 8.00, 0.50),
    "gpt-4.1-mini": Rates(0.40, 1.60, 0.10),
    "gpt-4.1-nano": Rates(0.10, 0.40, 0.025),
    "text-embedding-3-small": Rates(0.02, 0.0),
    "text-embedding-3-large": Rates(0.13, 0.0),
}

# Tool types, per API: run at the provider and only read; run at the provider and can
# change data; run by the agent, so a gate must see them.
RESPONSES_READ = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "image_generation",
    "programmatic_tool_calling",
}
RESPONSES_AGENT = {
    "function",
    "custom",
    "local_shell",
    "shell",
    "apply_patch",
    "computer",
    "computer_use_preview",
}
CHAT_AGENT = {"function", "custom"}
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")
MAX_TOKENS = 1_000_000_000  # a larger count is not a count


def op_for(path: str, method: str) -> str | None:
    """ "chat" for a chat path, the read kind for a read path, else None. Only a POST is."""
    if method != "POST":
        return None
    clean = path.split("?", 1)[0].rstrip("/")
    if clean.endswith(CHAT_PATHS):
        return "chat"
    for suffix, op in READ_PATHS.items():
        if clean.endswith(suffix):
            return op
    return None


def broken_body(path: str, op: str) -> bytes:
    """bad body's bytes for this call. A moderation or a token count gets a chat reply's."""
    if op == "embeddings":
        return BROKEN_EMBEDDING
    if path.split("?", 1)[0].rstrip("/").endswith("/responses"):
        return BROKEN_RESPONSE
    return BROKEN_BODY


def error_body(status: int) -> bytes:
    error = {
        "message": MESSAGES.get(status, f"The request failed with status {status}."),
        "type": TYPES.get(status, "server_error" if status >= 500 else "invalid_request_error"),
        "param": None,
        "code": CODES.get(status),
    }
    return json.dumps({"error": error}).encode()


def request_fields(body: bytes) -> dict[str, object] | None:
    """A request's JSON fields, or None when the body cannot be read as a JSON object."""
    try:
        value: object = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, not text, or empty
        return None
    return _obj(value) or None


def model_name(fields: dict[str, object] | None) -> str | None:
    """The model a request names, or None when it names none that can be read."""
    value = (fields or {}).get("model")
    return value if isinstance(value, str) and value else None


def max_output(fields: dict[str, object]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS:
            return value
    return None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    known: bool = False
    cached: int = 0  # input tokens the provider served from its prompt cache


def usage(body: bytes, *, output_required: bool = True) -> Usage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    A stream's lines may start with "event:" or be a ": ping", so only "data:" lines are
    read. Counts are trusted only when they are sensible whole numbers. An embedding has
    no output count; anything else without one has no usable counts. A cached count that
    is not a sensible number up to the input count is read as 0, which prices it high.
    """
    text = body.decode("utf-8", "replace")
    # Split on newlines only: text in a reply can hold other line separators.
    data = [line[5:].strip() for line in text.split("\n") if line.startswith("data:")]
    found = Usage()
    for doc in data or [text]:
        try:
            parsed = _obj(json.loads(doc))
        except (ValueError, RecursionError):
            continue
        numbers = _obj(parsed.get("usage"))
        if not numbers:  # the Responses API puts it in its last event's response
            numbers = _obj(_obj(parsed.get("response")).get("usage"))
        tokens_in = _count(numbers, "prompt_tokens", "input_tokens")
        tokens_out = _count(numbers, "completion_tokens", "output_tokens")
        if tokens_out is None and not output_required:
            tokens_out = 0
        if tokens_in is not None and tokens_out is not None:
            found = Usage(tokens_in, tokens_out, True, cached_count(numbers, tokens_in))
    return found


def cached_count(numbers: dict[str, object], tokens_in: int) -> int:
    """Chat names the block prompt_tokens_details, Responses input_tokens_details."""
    details = _obj(numbers.get("prompt_tokens_details", numbers.get("input_tokens_details")))
    cached = _count(details, "cached_tokens", "cached_tokens")
    return cached if cached is not None and cached <= tokens_in else 0


def price(model: str, overrides: dict[str, Rates]) -> Rates | None:
    """The team's price first, whole, then the built-in table.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. Nothing
    else does: gpt-4o-audio-preview is not priced like gpt-4o.
    """
    family = DATED.sub("", model)
    for name in (model, family):
        for table in (overrides, PRICES):
            if name in table:
                return table[name]
    return None


def cost(tokens_in: int, tokens_out: int, rates: Rates, cached: int = 0) -> float:
    """What a call costs. cached is the part of tokens_in served from the prompt cache."""
    cached_rate = rates.input if rates.cached_input is None else rates.cached_input
    full = (tokens_in - cached) * rates.input + cached * cached_rate
    return (full + tokens_out * rates.output) / 1_000_000


@dataclass
class Offered:
    """The tools a model request offers, sorted."""

    agent: list[str] = field(default_factory=list[str])  # names the agent runs itself
    hosted_reads: list[str] = field(default_factory=list[str])  # provider tools that only read
    hosted_writes: list[str] = field(default_factory=list[str])  # MCP server labels
    unknown: list[str] = field(default_factory=list[str])  # tool types not in the table


def offered(path: str, fields: dict[str, object]) -> Offered:
    found = Offered()
    responses = path.split("?", 1)[0].rstrip("/").endswith("/responses")
    if not responses:
        found.agent += [_name(_obj(f)) for f in _list(fields.get("functions"))]  # the old form
        if fields.get("web_search_options") is not None:
            found.hosted_reads.append("web_search")
    for raw in _list(fields.get("tools")):
        tool = _obj(raw)
        kind = str(tool.get("type") or "(no type)")
        if not responses:
            if kind in CHAT_AGENT:
                found.agent.append(_name(_obj(tool.get(kind)) or tool))
            else:
                found.unknown.append(kind)
            continue
        if kind in RESPONSES_READ:
            found.hosted_reads.append(kind)
            continue
        if kind == "mcp":
            found.hosted_writes.append(str(tool.get("server_label", "")))
        elif kind == "tool_search":
            if tool.get("execution") == "client":
                found.agent.append(_name(tool))
            else:
                found.hosted_reads.append(kind)
        elif kind == "namespace":
            found.agent += [_name(_obj(inner)) for inner in _list(tool.get("tools"))]
        elif kind in RESPONSES_AGENT:
            found.agent.append(_name(tool))
        else:
            found.unknown.append(kind)
    return found


def _name(tool: dict[str, object]) -> str:
    """A built-in tool such as shell has no name of its own: its type is the name."""
    return str(tool.get("name") or tool.get("type", ""))


def _obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def _list(value: object) -> list[object]:
    return list(cast(list[object], value)) if isinstance(value, list) else []


def _count(numbers: dict[str, object], in_chat: str, in_responses: str) -> int | None:
    """A token count, or None when the reply has none or it is not a sensible number."""
    value = numbers.get(in_chat, numbers.get(in_responses))
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        return None
    return value
