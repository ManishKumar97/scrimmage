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
TYPES = {429: "requests"}
CODES = {429: "rate_limit_exceeded", 401: "invalid_api_key"}

# Dollars per million tokens: (input, output). Check against OpenAI's pricing page
# before each release; a team overrides or adds a model under [agent.prices].
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
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


def usage(body: bytes, *, output_required: bool = True) -> Usage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    A stream's lines may start with "event:" or be a ": ping", so only "data:" lines are
    read. Counts are trusted only when they are sensible whole numbers. An embedding has
    no output count; anything else without one has no usable counts.
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
            found = Usage(tokens_in, tokens_out, True)
    return found


def price(model: str, overrides: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """The team's price first, then the built-in table.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. Nothing
    else does: gpt-4o-audio-preview is not priced like gpt-4o.
    """
    family = DATED.sub("", model)
    for name in (model, family):
        for table in (overrides, PRICES):
            if name in table:
                return table[name]
    return None


def cost(tokens_in: int, tokens_out: int, rates: tuple[float, float]) -> float:
    return (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000


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
