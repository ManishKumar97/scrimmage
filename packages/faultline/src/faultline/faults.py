"""Fault specs: what Reproduce breaks on purpose, written as short text.

    tool:NAME:KIND       one tool's calls over MCP, such as tool:get_logs:empty
    model:KIND[@STEP]    one model step, such as model:rate_limit@2 (default: step 1)

Each kind is one entry in TOOL_FAULTS or MODEL_FAULTS. A new fault is a new entry:
the parser, the proxy and the MCP wrapper do not change.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass


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


def empty_value(schema: dict) -> object:
    """The empty instance of a JSON schema: "", [], {} with each required field empty.

    Raises ValueError for a schema with no empty instance, such as a required number.
    """
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
                name: empty_value(properties.get(name, {}))
                for name in schema.get("required", [])
            }
    raise ValueError(f"no empty value for a {kind} field")


def empty_result(request_id: object, session: ToolSession) -> dict:
    """A valid MCP success with nothing in it: the tool ran and found nothing."""
    result: dict = {"content": [], "isError": False}
    if session.output_schema is not None:
        try:
            result["structuredContent"] = empty_value(session.output_schema)
        except ValueError:
            pass  # no empty instance exists, so the client will reject the result
    if session.tags_results:
        result["resultType"] = "complete"
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rate_limit(path: str) -> ModelAnswer:
    """HTTP 429 with the provider's own error body, so its SDK treats it as real."""
    message = "Rate limit reached. Injected by faultline."
    if path.rstrip("/").endswith("/messages"):  # Anthropic's Messages API
        error = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": message},
        }
    else:  # the OpenAI shape, which OpenAI-compatible providers share
        error = {
            "error": {
                "message": message,
                "type": "requests",
                "param": None,
                "code": "rate_limit_exceeded",
            }
        }
    headers = {"Content-Type": "application/json", "Retry-After": "1"}
    return ModelAnswer(429, headers, json.dumps(error).encode())


# The fault kinds. A tool fault answers a tools/call by its JSON-RPC id.
# A model fault answers a model request by its path.
TOOL_FAULTS: dict[str, Callable[[object, ToolSession], dict]] = {"empty": empty_result}
MODEL_FAULTS: dict[str, Callable[[str], ModelAnswer]] = {"rate_limit": rate_limit}


@dataclass(frozen=True)
class ToolFault:
    spec: str
    tool: str
    kind: str

    def answer(self, request_id: object, session: ToolSession) -> dict:
        return TOOL_FAULTS[self.kind](request_id, session)


@dataclass(frozen=True)
class ModelFault:
    spec: str
    kind: str
    step: int

    def answer(self, path: str) -> ModelAnswer:
        return MODEL_FAULTS[self.kind](path)


type Fault = ToolFault | ModelFault


def known() -> list[str]:
    tools = [f"tool:NAME:{kind}" for kind in TOOL_FAULTS]
    return tools + [f"model:{kind}[@STEP]" for kind in MODEL_FAULTS]


def parse(spec: str) -> Fault:
    """One fault from its spec. Raises FaultError naming the known faults."""
    target, _, rest = spec.partition(":")
    if target == "tool":
        tool, _, kind = rest.rpartition(":")
        if tool and kind in TOOL_FAULTS:
            return ToolFault(spec, tool, kind)
    elif target == "model":
        kind, at, step = rest.partition("@")
        if kind in MODEL_FAULTS and (not at or (step.isdigit() and int(step) >= 1)):
            return ModelFault(spec, kind, int(step) if at else 1)
    raise FaultError(f"unknown fault {spec!r}. Known: {', '.join(known())}")


def parse_all(specs: Iterable[str]) -> list[Fault]:
    return [parse(spec) for spec in specs]
