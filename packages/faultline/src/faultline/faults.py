"""Fault specs: what Reproduce breaks on purpose, written as short text.

    tool:NAME:KIND[@CALL]  one tool's calls over MCP, such as tool:get_logs:empty
                           (default: every call; @2 is the tool's second call)
    model:KIND[@STEP]      one model step, such as model:rate_limit@2 (default: step 1)

Each kind is one entry in TOOL_FAULTS or MODEL_FAULTS plus its injection code. A new
fault is a new entry: the parser does not change.

Limit: a tool's calls are counted by the MCP wrapper in front of its server. An agent
that restarts the server mid-run starts the count again.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from faultline.proxy.truncate import truncated


class FaultError(ValueError):
    """A fault spec that names no known fault."""


@dataclass(frozen=True)
class ModelAnswer:
    """The HTTP reply the proxy sends instead of the upstream's."""

    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class ToolSession:
    """What the MCP wrapper learned from the server, so a faked result passes for real.

    A client checks a result against the session: under MCP 2026-07-28 each result
    carries `resultType`, and a tool that declares an `outputSchema` must return
    `structuredContent` that fits it. A result without them reaches the agent as a
    protocol error, not as the fault.
    """

    output_schema: dict | None = None  # from tools/list
    tags_results: bool = False  # the server's results carry resultType


@dataclass(frozen=True)
class ToolRequest:
    """One tools/call request, as the MCP wrapper saw it."""

    request_id: object  # the JSON-RPC id
    tool: str
    arguments: object
    number: int  # the call's number among this tool's calls, from 1
    session: ToolSession = ToolSession()


@dataclass(frozen=True)
class Injection:
    """What a tool fault does to one call."""

    answer: dict | None = None  # the JSON-RPC message the client gets from the fault
    hang: bool = (
        False  # the client gets no answer at all; the server never sees the call
    )


class ToolInjector:
    """One tool fault's injection code, over one MCP server's life.

    The wrapper makes one per fault and asks it about each call of the fault's tool.
    A kind that keeps state, such as results it saw, keeps it on the instance.
    """

    def __init__(self, fault: "ToolFault") -> None:
        self.fault = fault

    def targets(self, call: ToolRequest) -> bool:
        """The selector: the Nth call of the tool, or every call."""
        return self.fault.call is None or self.fault.call == call.number

    def request(self, call: ToolRequest) -> Injection | None:
        """Before the server sees the call. None lets it through unchanged."""
        return None


class FaultShapeError(ValueError):
    """The fault cannot take a shape the client will accept. Holds the best answer."""

    def __init__(self, message: str, answer: dict) -> None:
        super().__init__(message)
        self.answer = answer


MAX_REF_DEPTH = 32  # a schema that refers to itself forever has no empty value


def empty_value(schema: dict, root: dict | None = None, depth: int = 0) -> object:
    """The empty instance of a JSON schema: "", [], {} with each required field empty.

    Local references ($ref to #/$defs/... or #/definitions/...) are followed, and
    the first anyOf or oneOf branch with an empty instance is taken.
    Raises ValueError for a schema with no empty instance, such as a required number.
    """
    root = schema if root is None else root
    if depth > MAX_REF_DEPTH:
        raise ValueError("the schema refers to itself with no end")
    if "$ref" in schema:
        return empty_value(resolve(schema["$ref"], root), root, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            problems = []
            for branch in schema[key]:
                try:
                    return empty_value(branch, root, depth + 1)
                except ValueError as exc:
                    problems.append(str(exc))
            raise ValueError(
                f"no {key} branch has an empty value ({'; '.join(problems)})"
            )
    kind = schema.get("type", "object" if "properties" in schema else None)
    kinds = kind if isinstance(kind, list) else [kind]
    if "null" in kinds:
        return None
    match kinds[0]:
        case "string":
            return ""
        case "array":
            return []
        case "object":
            properties = schema.get("properties", {})
            return {
                name: empty_value(properties.get(name, {}), root, depth + 1)
                for name in schema.get("required", [])
            }
    raise ValueError(f"no empty value for a {kind} field")


def resolve(ref: str, root: dict) -> dict:
    """A local JSON pointer reference, such as #/$defs/Row."""
    if not ref.startswith("#"):
        raise ValueError(f"cannot follow a non-local reference {ref}")
    node: object = root
    for token in ref.removeprefix("#").strip("/").split("/") if ref != "#" else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise ValueError(f"reference {ref} points at nothing")
        node = node[token]
    if isinstance(node, dict):
        return node
    raise ValueError(f"reference {ref} is not a schema")


def empty_result(request_id: object, session: ToolSession) -> dict:
    """A valid MCP success with nothing in it: the tool ran and found nothing.

    Raises FaultShapeError, holding the answer without structuredContent, when the
    tool's output schema has no empty instance: the client is then likely to reject it.
    """
    result: dict = {"content": [], "isError": False}
    if session.tags_results:
        result["resultType"] = "complete"
    answer = {"jsonrpc": "2.0", "id": request_id, "result": result}
    if session.output_schema is not None:
        try:
            result["structuredContent"] = empty_value(session.output_schema)
        except ValueError as exc:
            raise FaultShapeError(str(exc), answer) from None
    return answer


class Empty(ToolInjector):
    """tool:NAME:empty, the tool ran and found nothing. The server never sees the call."""

    def request(self, call: ToolRequest) -> Injection | None:
        if self.targets(call):
            return Injection(answer=empty_result(call.request_id, call.session))
        return None


class Error(ToolInjector):
    """tool:NAME:error, the tool failed. The server never sees the call.

    The answer is a tool error, as MCP sends one: a result with isError, so the
    model reads the message. It is not a JSON-RPC protocol error.
    """

    def request(self, call: ToolRequest) -> Injection | None:
        if not self.targets(call):
            return None
        text = (
            f"Error executing tool {call.tool}: the backend did not respond "
            "(connection reset by peer)"
        )
        result: dict = {"content": [{"type": "text", "text": text}], "isError": True}
        if call.session.tags_results:
            result["resultType"] = "complete"
        return Injection(
            answer={"jsonrpc": "2.0", "id": call.request_id, "result": result}
        )


class Timeout(ToolInjector):
    """tool:NAME:timeout, the tool hangs. The call is never answered.

    The agent's own timeout for the call is what ends it, as with a stalled backend.
    """

    def request(self, call: ToolRequest) -> Injection | None:
        return Injection(hang=True) if self.targets(call) else None


def rate_limit(path: str) -> ModelAnswer:
    """HTTP 429 with the provider's own error body and a retry-after header.

    Both SDKs read retry-after to time their next attempt, so the retries come fast.
    """
    return provider_error(
        path,
        429,
        "Rate limit reached. Injected by faultline.",
        ("requests", "rate_limit_exceeded"),
        "rate_limit_error",
        {"retry-after": "1"},
    )


def is_anthropic(path: str) -> bool:
    """True for Anthropic's Messages API. Every other model call is OpenAI-shaped."""
    return path.rstrip("/").endswith("/messages")


def provider_error(
    path: str,
    status: int,
    message: str,
    openai: tuple[str, str | None],
    anthropic: str,
    headers: dict[str, str] | None = None,
) -> ModelAnswer:
    """An HTTP error in the provider's own body shape, so its SDK raises its own error.

    `openai` is the error's type and code in the OpenAI shape, which OpenAI-compatible
    providers share. `anthropic` is the error type in Anthropic's shape.
    """
    if is_anthropic(path):
        error: dict = {
            "type": "error",
            "error": {"type": anthropic, "message": message},
        }
    else:
        kind, code = openai
        error = {
            "error": {"message": message, "type": kind, "param": None, "code": code}
        }
    headers = {"Content-Type": "application/json", **(headers or {})}
    return ModelAnswer(status, headers, json.dumps(error).encode())


def server_error(path: str) -> ModelAnswer:
    """HTTP 500, an outage on the provider's side. The SDKs retry it, then raise."""
    message = (
        "The server had an error while processing your request. Injected by faultline."
    )
    return provider_error(path, 500, message, ("server_error", None), "api_error")


@dataclass(frozen=True)
class ModelKind:
    """What one model fault kind does to the call it targets. Set exactly one field.

    answer: the reply the agent gets instead. The upstream never sees the call.
    hang: the call is never answered. The agent's own timeout ends it.
    rewrite: the call goes to the upstream, and the agent gets its successful reply
        changed. It takes the request path and the reply body, and raises Unchanged
        when it cannot apply.
    """

    answer: Callable[[str], ModelAnswer] | None = None  # from the request path
    hang: bool = False
    rewrite: Callable[[str, bytes], bytes] | None = None


def truncate(path: str, body: bytes) -> bytes:
    """The reply cut to half its text, marked as stopped at the token limit."""
    return truncated(body, anthropic=is_anthropic(path))


# The fault kinds. A tool kind is the injector the MCP wrapper asks about each call.
TOOL_FAULTS: dict[str, type[ToolInjector]] = {
    "empty": Empty,
    "error": Error,
    "timeout": Timeout,
}
MODEL_FAULTS: dict[str, ModelKind] = {
    "rate_limit": ModelKind(answer=rate_limit),
    "timeout": ModelKind(hang=True),
    "server_error": ModelKind(answer=server_error),
    "truncated": ModelKind(rewrite=truncate),
}


@dataclass(frozen=True)
class ToolFault:
    spec: str
    tool: str
    kind: str
    call: int | None = None  # the Nth call of the tool, or None for every call

    def injector(self) -> ToolInjector:
        """A new injector for this fault, with no state yet."""
        return TOOL_FAULTS[self.kind](self)


@dataclass(frozen=True)
class ModelFault:
    spec: str
    kind: str
    step: int

    @property
    def action(self) -> ModelKind:
        return MODEL_FAULTS[self.kind]


type Fault = ToolFault | ModelFault


def known() -> list[str]:
    tools = [f"tool:NAME:{kind}[@CALL]" for kind in TOOL_FAULTS]
    return tools + [f"model:{kind}[@STEP]" for kind in MODEL_FAULTS]


def selector(rest: str) -> tuple[str, int | None]:
    """Split a trailing @N off a spec. Raises FaultError for an @ with no N of 1 or more."""
    head, at, number = rest.rpartition("@")
    if not at:
        return rest, None
    if number.isdigit() and int(number) >= 1:
        return head, int(number)
    raise FaultError(f"@{number} is not a step or call number (1 or more)")


def parse(spec: str) -> Fault:
    """One fault from its spec. Raises FaultError naming the known faults."""
    target, _, rest = spec.partition(":")
    try:
        rest, number = selector(rest)
    except FaultError as exc:
        raise FaultError(
            f"unknown fault {spec!r}: {exc}. Known: {', '.join(known())}"
        ) from None
    if target == "tool":
        tool, _, kind = rest.rpartition(":")
        if tool and kind in TOOL_FAULTS:
            return ToolFault(spec, tool, kind, number)
    elif target == "model" and rest in MODEL_FAULTS:
        return ModelFault(spec, rest, number or 1)
    raise FaultError(f"unknown fault {spec!r}. Known: {', '.join(known())}")


def parse_all(specs: Iterable[str]) -> list[Fault]:
    return [parse(spec) for spec in specs]
