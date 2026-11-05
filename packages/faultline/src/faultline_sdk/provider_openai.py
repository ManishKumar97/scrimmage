"""OpenAI's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body. The gate uses
them to label a call, build a fault the openai library reads as a real error, count
tokens and sort the tools the model is offered.
"""

import json
from dataclasses import dataclass, field
from typing import cast

HOSTS = ("api.openai.com",)
CHAT_PATHS = ("/chat/completions", "/responses")
READ_PATHS = {"/embeddings": "embeddings"}  # provider calls that only read or compute

# The 429 body: the spec's Error object; the message as OpenAI's rate-limit cookbook
# shows it; the code from the spec's error code list.
MESSAGES = {
    400: "Invalid request.",
    401: "Incorrect API key provided.",
    403: "You are not allowed to make this request.",
    404: "The model does not exist or you do not have access to it.",
    429: (
        "Rate limit reached for default-codex in organization org-{id} on requests per "
        "min. Limit: 20.000000 / min. Current: 24.000000 / min. Contact support@openai.com "
        "if you continue to have issues or if you’d like to request an increase."  # noqa: RUF001  (OpenAI's own text)
    ),
    500: "The server had an error while processing your request. Sorry about that!",
    502: "Bad gateway.",
    503: "The engine is currently overloaded, please try again later.",
}
TYPES = {429: "requests", 401: "invalid_request_error", 400: "invalid_request_error"}
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


def op_for(path: str) -> str | None:
    """ "chat" for a chat path, the read kind for a read path, else None."""
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


def request_fields(body: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(body) if body else {}
    except ValueError:
        return {}
    return _obj(value)


def max_output(fields: dict[str, object]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    known: bool = False


def usage(body: bytes) -> Usage:
    """Token counts from a reply: a JSON body, or the chunks of a streamed one."""
    text = body.decode("utf-8", "replace")
    found = Usage()
    docs: list[str] = [text] if not text.lstrip().startswith("data:") else []
    if not docs:
        docs = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    for doc in docs:
        try:
            parsed = _obj(json.loads(doc))
        except ValueError:
            continue
        numbers = _obj(parsed.get("usage"))
        if not numbers:  # the Responses API puts it in its last event's response
            numbers = _obj(_obj(parsed.get("response")).get("usage"))
        if numbers:
            found.input = _int(numbers, "prompt_tokens", "input_tokens")
            found.output = _int(numbers, "completion_tokens", "output_tokens")
            found.known = True
    return found


def price(model: str, overrides: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """The team's price first, then the built-in table; a dated snapshot uses its family."""
    for table in (overrides, PRICES):
        if model in table:
            return table[model]
    for table in (overrides, PRICES):
        family = max((m for m in table if model.startswith(m + "-")), key=len, default=None)
        if family is not None:
            return table[family]
    return None


def cost(tokens_in: int, tokens_out: int, rates: tuple[float, float]) -> float:
    return (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000


@dataclass
class Offered:
    """The tools a model request offers, sorted."""

    agent: list[str] = field(default_factory=list[str])  # names the agent runs itself
    hosted_writes: list[str] = field(default_factory=list[str])  # MCP server labels
    unknown: list[str] = field(default_factory=list[str])  # tool types not in the table


def offered(path: str, fields: dict[str, object]) -> Offered:
    found = Offered()
    responses = path.split("?", 1)[0].rstrip("/").endswith("/responses")
    for raw in _list(fields.get("tools")):
        tool = _obj(raw)
        kind = str(tool.get("type", ""))
        if not responses:
            if kind in CHAT_AGENT:
                found.agent.append(_name(_obj(tool.get(kind)) or tool))
            else:
                found.unknown.append(kind)
            continue
        if kind in RESPONSES_READ:
            continue
        if kind == "mcp":
            found.hosted_writes.append(str(tool.get("server_label", "")))
        elif kind == "tool_search":
            if tool.get("execution") == "client":
                found.agent.append(_name(tool))
        elif kind == "namespace":
            found.agent += [_name(_obj(inner)) for inner in _list(tool.get("tools"))]
        elif kind in RESPONSES_AGENT:
            found.agent.append(_name(tool))
        else:
            found.unknown.append(kind)
    return found


def _name(tool: dict[str, object]) -> str:
    return str(tool.get("name", ""))


def _obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def _list(value: object) -> list[object]:
    return list(cast(list[object], value)) if isinstance(value, list) else []


def _int(numbers: dict[str, object], *keys: str) -> int:
    for key in keys:
        value = numbers.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0
