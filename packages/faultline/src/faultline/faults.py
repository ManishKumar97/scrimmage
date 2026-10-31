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
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.model import Run
from faultline.proxy.truncate import truncated
from faultline.runs import read_runs_many


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
    # The client gets no answer at all, and the server never sees the call.
    hang: bool = False
    # The fault targeted the call but left it as it was, and why.
    skipped: str | None = None
    value: str | None = None  # the value the fault put in, recorded with the call
    argument: str | None = None  # the argument whose value it replaced


@dataclass(frozen=True)
class ToolOptions:
    """What `faultline run` tells every tool fault in the run."""

    stale_from: Path | None = None  # the trace directory of an earlier run


class ToolInjector:
    """One tool fault's injection code, over one MCP server's life.

    The wrapper makes one per fault and asks it about each call of the fault's tool.
    A kind that keeps state, such as results it saw, keeps it on the instance.
    """

    takes_value = False  # the kind is written KIND=VALUE

    def __init__(
        self, fault: "ToolFault", options: ToolOptions = ToolOptions()
    ) -> None:
        self.fault = fault
        self.options = options

    def targets(self, call: ToolRequest) -> bool:
        """The selector: the Nth call of the tool, or every call."""
        return self.fault.call is None or self.fault.call == call.number

    def request(self, call: ToolRequest) -> Injection | None:
        """Before the server sees the call. None lets it through unchanged."""
        return None

    def response(self, call: ToolRequest, message: dict) -> Injection | None:
        """After the server answered a call this injector let through."""
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


class Stale(ToolInjector):
    """tool:NAME:stale, the tool answers with an old result, as a success.

    Always for the same tool and arguments. From an earlier run when --stale-from
    names its trace: the result recorded there. Else from this run: the first
    successful result for those arguments, remembered as it passed through. A
    targeted call with neither passes through, and its span says nothing was injected.

    A result counts once the server has answered it. Calls made at the same moment
    as the first one with their arguments find nothing remembered yet, and pass through.
    A result the earlier run's trace cut to its content limit is served as cut.
    """

    def __init__(
        self, fault: "ToolFault", options: ToolOptions = ToolOptions()
    ) -> None:
        super().__init__(fault, options)
        # The first successful result in this run for each set of arguments.
        self.first: dict[str, dict] = {}
        self.earlier: dict[str, str] = {}
        if options.stale_from:
            self.earlier = self.load(options.stale_from)

    def load(self, trace: Path) -> dict[str, str]:
        """The earlier run's results for the tool. Never raises: a trace that cannot
        be read is a warning, and the fault serves only results from this run."""
        files, problems = span_files(trace)
        for problem in problems:
            warn(f"--stale-from {trace}: skipped {problem}")
        try:
            runs = read_runs_many(files).runs
        except OSError as exc:
            warn(f"--stale-from {trace}: cannot read it ({exc})")
            runs = []
        found = earlier_results(runs, self.fault.tool)
        if not runs:
            warn(
                f"--stale-from {trace} holds no run; {self.fault.spec} serves only "
                "results from this run"
            )
        elif not found:
            warn(
                f"--stale-from {trace} holds no successful result of "
                f"{self.fault.tool}; {self.fault.spec} serves only results from this run"
            )
        return found

    def request(self, call: ToolRequest) -> Injection | None:
        if not self.targets(call):
            return None
        asked = arguments_key(call.arguments)
        text = self.earlier.get(asked)
        if text is not None:
            return Injection(answer=text_result(call, text))
        if asked in self.first:
            result = self.first[asked]
            return Injection(
                answer={"jsonrpc": "2.0", "id": call.request_id, "result": result}
            )
        return None

    def response(self, call: ToolRequest, message: dict) -> Injection | None:
        result = message.get("result")
        if isinstance(result, dict) and not result.get("isError"):
            self.first.setdefault(arguments_key(call.arguments), result)
        if not self.targets(call):
            return None
        where = " or in the earlier run" if self.options.stale_from else ""
        return Injection(
            skipped=f"no earlier result of {call.tool} for these arguments "
            f"in this run{where} to serve"
        )


class Wrong(ToolInjector):
    """tool:NAME:wrong=VALUE, the tool answers about the wrong thing, as a success.

    The call goes to the server for real. One argument names what the call asked
    about: the argument `service` when the call has a string one, else the call's
    first string argument. In the result, each whole-word occurrence of that
    argument's value becomes VALUE. A word here is letters, digits and underscores,
    so "ad" in the pod name "ad-5d9f7" is replaced and "ad" in "loaded" or "ads" is
    not. The data stays well formed: it now names another service. The span records
    the value and the argument used.
    """

    takes_value = True

    def response(self, call: ToolRequest, message: dict) -> Injection | None:
        if not self.targets(call):
            return None
        result = message.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            return Injection(skipped="the tool failed, so there was no data to change")
        value = self.fault.value or ""
        named = asked_about(call.arguments)
        if named is None:
            return Injection(skipped="the call passed no string argument to replace")
        argument, asked = named
        if asked == value:
            return Injection(skipped=f"the call already asked about {value}")
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(asked)}(?![A-Za-z0-9_])")
        changed = replaced(result, pattern, value)
        if changed == result:
            return Injection(skipped=f"the {argument} {asked!r} is not in the result")
        return Injection(
            answer={**message, "result": changed}, value=value, argument=argument
        )


# The argument that names what a call asks about, when the call has it.
ASKED_ABOUT = "service"


def asked_about(arguments: object) -> tuple[str, str] | None:
    """The name and value of the argument the call asks about, or None.

    `service` when it is a non-empty string, else the first non-empty string argument.
    """
    if not isinstance(arguments, dict):
        return None
    strings = {k: v for k, v in arguments.items() if isinstance(v, str) and v}
    if ASKED_ABOUT in strings:
        return ASKED_ABOUT, strings[ASKED_ABOUT]
    return next(iter(strings.items()), None)


def replaced(value: object, pattern: re.Pattern[str], new: str) -> object:
    """A JSON value with every match in its strings replaced, in one pass."""
    if isinstance(value, str):
        return pattern.sub(lambda _: new, value)
    if isinstance(value, dict):
        return {key: replaced(item, pattern, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replaced(item, pattern, new) for item in value]
    return value


def arguments_key(arguments: object) -> str:
    """Arguments as one string, the same whatever their key order or spacing."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return arguments
    return json.dumps(arguments, sort_keys=True)


def warn(message: str) -> None:
    """A warning from a tool fault, on the MCP wrapper's stderr."""
    print(f"faultline mcp-wrap: {message}", file=sys.stderr)


def span_files(trace: Path) -> tuple[list[Path], list[str]]:
    """The span files (*.jsonl) of a trace directory that can be opened, and why
    each other one cannot, such as a directory or a symlink loop by that name."""
    if not trace.is_dir() or not os.access(trace, os.R_OK | os.X_OK):
        return [], [f"{trace} itself: not a readable directory"]
    readable, problems = [], []
    for path in sorted(trace.glob("*.jsonl")):
        try:
            with path.open("rb"):
                pass
        except OSError as exc:
            problems.append(f"{path.name}: {exc.strerror or exc}")
            continue
        readable.append(path)
    return readable, problems


def earlier_results(runs: Iterable[Run], tool: str) -> dict[str, str]:
    """The tool's successful results in an earlier run's trace, by arguments.

    A result a fault made is left out: it is not what the tool said. The first
    result for the same arguments wins.
    """
    found: dict[str, str] = {}
    for run in runs:
        for call in run.tool_calls:
            if call.name == tool and call.error_type is None and not call.injected:
                found.setdefault(arguments_key(call.arguments), call.result)
    return found


def text_result(call: ToolRequest, text: str) -> dict:
    """A success holding recorded text, in the shape the session expects.

    Raises FaultShapeError, holding the answer without structuredContent, when the
    tool's output schema cannot be filled from the text.
    """
    result: dict = {"content": [{"type": "text", "text": text}], "isError": False}
    if call.session.tags_results:
        result["resultType"] = "complete"
    answer = {"jsonrpc": "2.0", "id": call.request_id, "result": result}
    schema = call.session.output_schema
    if schema is not None:
        try:
            result["structuredContent"] = structured_from_text(schema, text)
        except ValueError as exc:
            raise FaultShapeError(str(exc), answer) from None
    return answer


def structured_from_text(schema: dict, text: str) -> object:
    """The structuredContent a tool with this output schema gives with this text.

    Two shapes are known: one required string field, as MCP SDKs wrap a tool that
    returns a string, and an object the text holds as JSON.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if len(required) == 1 and properties.get(required[0], {}).get("type") == "string":
        return {required[0]: text}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    raise ValueError("the recorded text does not fill the tool's output schema")


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
    "stale": Stale,
    "wrong": Wrong,
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
    value: str | None = None  # the VALUE of a kind written KIND=VALUE

    def injector(self, options: ToolOptions = ToolOptions()) -> ToolInjector:
        """A new injector for this fault, with no state yet."""
        return TOOL_FAULTS[self.kind](self, options)


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
    tools = [
        f"tool:NAME:{kind}{'=VALUE' if injector.takes_value else ''}[@CALL]"
        for kind, injector in TOOL_FAULTS.items()
    ]
    return tools + [f"model:{kind}[@STEP]" for kind in MODEL_FAULTS]


def selector(rest: str) -> tuple[str, int | None]:
    """Split a trailing @N off a spec. Raises FaultError for @0.

    An @ not followed by digits stays: it may be part of a value, as in wrong=a@b.
    """
    head, at, number = rest.rpartition("@")
    if not at or not number.isdigit():
        return rest, None
    if int(number) >= 1:
        return head, int(number)
    raise FaultError(f"@{number} is not a step or call number (1 or more)")


def unsafe_in(value: str) -> str | None:
    """The first character a fault value may not hold, described, or None."""
    for char in value:
        if char == '"':
            return "a double quote"
        if char == "\\":
            return "a backslash"
        if ord(char) < 32 or ord(char) == 127:
            return f"the control character {char!r}"
    return None


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
        # The value comes after the first =, so it may hold a colon.
        named, has_value, value = rest.partition("=")
        tool, _, kind = named.rpartition(":")
        if (
            tool
            and kind in TOOL_FAULTS
            and TOOL_FAULTS[kind].takes_value == bool(has_value)
            and (value or not has_value)
        ):
            if unsafe := unsafe_in(value):
                raise FaultError(
                    f"fault {spec!r}: the value may not hold {unsafe}. It goes into "
                    "tool results as it is, where a quote, a backslash or a control "
                    "character would break JSON the tool wrote as text"
                )
            return ToolFault(spec, tool, kind, number, value or None)
    elif target == "model" and rest in MODEL_FAULTS:
        return ModelFault(spec, rest, number or 1)
    raise FaultError(f"unknown fault {spec!r}. Known: {', '.join(known())}")


def parse_all(specs: Iterable[str]) -> list[Fault]:
    return [parse(spec) for spec in specs]
