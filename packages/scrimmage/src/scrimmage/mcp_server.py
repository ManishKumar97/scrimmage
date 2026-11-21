"""The Scrimmage MCP server: a coding agent runs scrimmage check, init, record, test and show.

Each tool call starts `python -P -m scrimmage <command> ... --json` as a child process on
this server's own Python, and returns the child's one JSON document. A command never runs in
this process, so its signal handlers, its imports and its working folder stay its own.
The server speaks MCP over stdio only: the client starts it, and nothing listens on a socket.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

from scrimmage import versions
from scrimmage.redact import redact

INSTALL = 'scrimmage-mcp needs the mcp extra; install it with: pip install "scrimmage[mcp]"'

try:
    import anyio
    import anyio.abc
    import jsonschema
    import mcp_types as types
    from jsonschema.exceptions import ValidationError
    from mcp.server.context import ServerRequestContext
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
except ImportError:  # this module is only the scrimmage-mcp command: say why, and stop
    print(INSTALL, file=sys.stderr)
    raise SystemExit(2) from None

MISTAKE, INFRA = 2, 3  # the exit codes that make a result an error; 1 is a result, not one
DOC_CAP = 16 * 2**20  # a child's standard output kept whole up to this; more is a failure
STDERR_KEPT = 64 * 1024  # the tail of a child's standard error kept, never forwarded
MAX_CHILDREN = 4  # children at once, in all; a fifth call waits for a slot
KILL_REAP_S = 5.0  # after SIGKILL, how long to wait for the pipes to close
TAIL_CHARS = 200  # of a child's last line on standard error, as show cuts a log line
# How long a stopped child has to print its document: the runner's own grace to stop its
# runs, KILL_GRACE_S, plus 10 s for the report on a loaded machine. Written out, as this
# module never imports the runner.
STOP_GRACE_S = 15.0
CHECK_LIMIT_S = 60.0  # the check before a spend reads one file
PROGRESS_EVERY_S = 5.0
EXIT_SLACK_S = 5.0  # after the longest stop, before a shutting-down server exits anyway
CALL_VARIABLE = "SCRIMMAGE_MCP_CALL"  # set on each child, so every process it starts holds it
COMMANDS = ("check", "test", "record", "init", "show")
INSTRUCTIONS = (
    "check and show only read files: they run nothing and cost nothing. test and record run "
    "the team's agent, which spends money on model calls, up to the cost cap each result "
    "names. Call check before test. Act on exit, status and the other fields of the JSON "
    "document, never on the wording of a message."
)

# Input schemas. The server checks every call against the same dict the client sees.
PATH = {"type": "string", "minLength": 1, "maxLength": 1024}
NAME = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "pattern": "^[^-\\u0000-\\u001f\\u007f][^\\u0000-\\u001f\\u007f]*$",
}
RUN_ID = re.compile(r"run-[0-9a-f]{12}")
PROBE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
USD = {"type": "number", "exclusiveMinimum": 0, "maximum": 1000}


def described(schema: Mapping[str, object], text: str) -> dict[str, object]:
    return {**schema, "description": text}


COMPARE = described(PATH, "An earlier records folder, or its result.json, to compare with.")
MAX_COST = described(
    USD,
    "The most this call may spend on model calls, in dollars. The cap used is the lower of "
    "this and the file's max_cost_usd.",
)
PROPERTIES: dict[str, dict[str, dict[str, object]]] = {
    "check": {},
    "test": {"max_cost_usd": MAX_COST, "compare": COMPARE},
    "record": {
        "max_cost_usd": MAX_COST,
        "tests": described(
            {"type": "array", "items": NAME, "maxItems": 100, "uniqueItems": True},
            "Record only these tests, by name.",
        ),
        "refresh": described(
            {"type": "array", "items": NAME, "maxItems": 100, "uniqueItems": True},
            "Record these labels again, though they already have a mock.",
        ),
        "runs": described(
            {"type": "integer", "minimum": 1, "maximum": 1000},
            "Run each test's baseline this many times.",
        ),
    },
    "init": {"folder": described(PATH, "The folder of the agent's code; the config's by default.")},
    "show": {
        "target": described(PATH, "A records folder or its result.json; the newest by default."),
        "run": described(
            {"type": "string", "pattern": f"^{RUN_ID.pattern}$"}, "Show only this run."
        ),
        "compare": COMPARE,
    },
}
PROBE_PROPERTY = described(
    {"type": "string", "pattern": f"^{PROBE.pattern}$"},
    "module:function to run up to its first model request, which is refused before it is "
    "sent, to read the tools it offers. Importing the module runs its top-level code.",
)
REQUIRED = {"test": ["max_cost_usd"], "record": ["max_cost_usd"]}
DESCRIPTIONS = {
    "check": (
        "Scrimmage: check scrimmage.toml the way test does before its first run. Reads the file "
        "only: it runs nothing and costs nothing. The result names the cost cap, the entry "
        "point and the labels under real."
    ),
    "test": (
        "Scrimmage: run every test in scrimmage.toml against the team's agent. Runs the agent's "
        "code, which spends money on model calls, up to max_cost_usd, and calls for real every "
        "label under real. Call check first."
    ),
    "record": (
        "Scrimmage: run each test's baseline and write its real calls as mocks in "
        "scrimmage.recorded.toml, which it replaces whole. Runs the agent's code and spends "
        "money on model calls, up to max_cost_usd."
    ),
    "init": (
        "Scrimmage: draft a first scrimmage.toml by reading the agent's code. Never replaces "
        "a file."
    ),
    "show": (
        "Scrimmage: say what happened in a finished test, read from its records folder. Runs "
        "nothing and costs nothing."
    ),
}


def hints(name: str, probe: bool) -> types.ToolAnnotations:
    if name in ("check", "show"):
        return types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
    if name == "init":
        return types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=probe,
        )
    return types.ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    )


def input_schema(name: str, probe: bool) -> dict[str, object]:
    properties = dict(PROPERTIES[name])
    if name == "init" and probe:
        properties["probe"] = PROBE_PROPERTY
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if name in REQUIRED:
        schema["required"] = REQUIRED[name]
    return schema


def cli_schema() -> dict[str, Any]:
    """The schema of every command's --json document, as the package ships it."""
    text = resources.files("scrimmage").joinpath("schemas/cli.v1.json").read_text("utf-8")
    return cast(dict[str, Any], json.loads(text))


def references(node: object) -> set[str]:
    """Every #/$defs/ name a schema refers to, at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in cast(dict[str, object], node).items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                found.add(value.removeprefix("#/$defs/"))
            else:
                found |= references(value)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            found |= references(item)
    return found


def reachable(defs: Mapping[str, object], names: Sequence[str]) -> list[str]:
    """The definitions these names need, themselves included, in the file's order."""
    seen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += sorted(references(defs[name]) - seen)
    return [name for name in defs if name in seen]


def output_schema(command: str, schema: Mapping[str, Any]) -> dict[str, object]:
    """One command's document schema, built from the shipped one, so the two cannot drift."""
    defs = cast(dict[str, object], schema["$defs"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "allOf": [{"$ref": "#/$defs/envelope"}, {"$ref": f"#/$defs/{command}"}],
        "$defs": {name: defs[name] for name in reachable(defs, ["envelope", command])},
    }


OUTPUT_SCHEMAS = {command: output_schema(command, cli_schema()) for command in COMMANDS}


@dataclass(frozen=True)
class Settings:
    root: Path  # resolved once, at start
    config: Path  # absolute, inside the root
    max_session_usd: float
    allow_no_budget: bool
    allow_probe: bool
    call_timeout: float
    aliases: tuple[str, ...] = ()  # the root as the person gave it, when that differs


# A path in text ends at / or at a character no file name in a message holds.
AFTER = r"(?=/|$|[\s\"'`:,;)\]}>])"
BEFORE = r"(?<![\w.~/-])"  # and starts where no longer path name goes on before it


def path_rule(roots: Sequence[str], homes: Sequence[str]) -> Callable[[str], str]:
    """Replaces the root with . and the home folder with ~, only where a path starts and ends,
    the longest first: so with the root /home/u/app, /home/u/app/x is ./x, and /home/u/app2/x
    is ~/app2/x, never .2/x. A document then names paths the coding agent can open from its own
    working folder, and no folder of the person's outside the project."""
    swaps = {home: "~" for home in homes} | {root: "." for root in roots}
    order = sorted((form for form in swaps if form not in ("", "/")), key=len, reverse=True)
    if not order:
        return lambda value: value
    pattern = re.compile(BEFORE + "(" + "|".join(map(re.escape, order)) + ")" + AFTER)
    return lambda value: pattern.sub(lambda found: swaps[found.group(1)], value)


def every_string(node: object, change: Callable[[str], str]) -> Any:
    if isinstance(node, str):
        return change(node)
    if isinstance(node, list):
        return [every_string(item, change) for item in cast(list[object], node)]
    if isinstance(node, dict):
        return {k: every_string(v, change) for k, v in cast(dict[str, object], node).items()}
    return node


LISTS = ("mistakes", "notes", "warnings", "infra")


def forwarded(doc: Mapping[str, Any], paths: Callable[[str], str]) -> dict[str, Any]:
    """The document as the client gets it: the path rule on every string, and redact on every
    message, which the CLI writes as it finds it. Nothing else is redacted: a hash such as
    test_sha256 must reach a later compare unchanged, and the other fields are redacted where
    they are made."""
    out = cast(dict[str, Any], every_string(doc, paths))
    for name in LISTS:
        if isinstance(out.get(name), list):
            out[name] = [redacted_message(item) for item in cast(list[object], out[name])]
    return out


def redacted_message(item: object) -> object:
    if not isinstance(item, dict):
        return item
    message = cast(dict[str, object], item)
    text = message.get("message")
    return {**message, "message": redact(text)} if isinstance(text, str) else message


def cap_text(usd: object) -> str:
    return f"${usd:g}" if isinstance(usd, int | float) else "none"


def spent_text(usd: object) -> str:
    return f"${usd:.4f}" if isinstance(usd, int | float) else "$0.0000"


def counted(items: Sequence[Mapping[str, Any]], key: str, order: Sequence[str]) -> str:
    """How many items hold each value of key, in the given order, then any other."""
    seen = [str(item.get(key)) for item in items]
    values = [*order, *sorted(set(seen) - set(order))]
    return ", ".join(f"{seen.count(value)} {value}" for value in values if value in seen)


RESULTS = ("pass", "fail", "no result")
CHANGES = ("better", "worse", "unclear", "no result", "new", "gone")


def summary_lines(doc: Mapping[str, Any], guard: Mapping[str, Any]) -> list[str]:
    """What the result says first, in plain lines. Built from the document's fields only, never
    from message text, and in the judge's words: pass, fail, no result."""
    command = str(doc.get("command"))
    lines = [f"{command}: {doc.get('status')} (exit {doc.get('exit')})"]
    found = doc.get("summary") if command == "check" else None
    summary = cast(dict[str, Any], found) if isinstance(found, dict) else None
    real: object = guard.get("real") if summary is None else summary.get("real")
    real_line = (
        f"real: {', '.join(map(str, cast(list[object], real))) or 'nothing'}"
        if isinstance(real, list)
        else ""
    )
    if command == "check":
        if summary is not None:
            lines.append(
                f"tests {summary.get('tests')}, rows {summary.get('rows')}, "
                f"runs {summary.get('runs')}"
            )
            lines += [line for line in (entry_line(summary.get("entry")),) if line]
            lines.append(f"cost cap: {cap_text(summary.get('budget_usd'))}")
            lines.append(real_line or "real: nothing")
            lines.append(prices_line(summary.get("prices")))
    elif command == "test":
        rows = cast(list[Mapping[str, Any]], doc.get("rows") or [])
        lines.append(f"rows: {counted(rows, 'result', RESULTS) or 'none ran'}")
        lines.append(
            f"spent {spent_text(doc.get('cost_usd'))} of a {cap_text(doc.get('budget_usd'))} cap"
        )
        lines += [line for line in (entry_line(guard.get("entry")), real_line) if line]
        if doc.get("stopped_by") is not None:
            lines.append(f"stopped by: {doc['stopped_by']}")
        if doc.get("records") is not None:
            lines.append(f"records: {doc['records']}")
        lines += compared(doc)
    elif command == "record":
        numbers = cast(Mapping[str, Any], doc.get("numbers") or {})
        lines.append(
            f"calls written: {numbers.get('calls_written')} of {numbers.get('calls_seen')}"
        )
        lines.append(f"out: {doc.get('out') or 'nothing was written'}")
        lines.append(
            f"spent {spent_text(doc.get('cost_usd'))} of a {cap_text(doc.get('budget_usd'))} cap"
        )
        lines += [line for line in (entry_line(guard.get("entry")), real_line) if line]
        if doc.get("stopped_by") is not None:
            lines.append(f"stopped by: {doc['stopped_by']}")
    elif command == "init":
        lines.append(f"out: {doc.get('out') or 'nothing was written'}")
        entry = doc.get("entry")
        if isinstance(entry, dict):
            point = cast(dict[str, Any], entry)
            guess = " (a guess, never run)" if point.get("guess") else ""
            lines.append(f"entry: {point.get('kind')} {point.get('value')}{guess}")
        probe = doc.get("probe")
        if isinstance(probe, dict) and cast(dict[str, Any], probe).get("ran"):
            lines.append("probe ran your code up to its first model request")
    elif command == "show":
        runs = cast(list[Mapping[str, Any]], doc.get("runs") or [])
        lines.append(f"runs shown: {len(runs)}")
        for run in runs:
            for suggestion in cast(list[Mapping[str, Any]], run.get("suggestions") or []):
                lines.append(f"suggestion: {suggestion.get('code')} (a heuristic)")
        lines += compared(doc)
    counts = [f"{name} {len(doc.get(name) or [])}" for name in LISTS]
    lines.append("messages: " + ", ".join(counts))
    return lines


def prices_line(prices: object) -> str:
    """The prices the cap is measured at: the file's own, by model, and the built-in table's
    for any other. A price of 0 makes that model's calls free against the cap."""
    if not isinstance(prices, dict) or not prices:
        return "prices: the built-in table"
    table = cast(dict[str, dict[str, float]], prices)
    named = ", ".join(
        f"{model} ${rate['input']:g}/${rate['output']:g} per million tokens in/out"
        for model, rate in sorted(table.items())
    )
    return f"prices: {named}; any other model the built-in table"


def entry_line(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    point = cast(dict[str, Any], entry)
    return f"entry: {point.get('kind')} {point.get('value')}"


def compared(doc: Mapping[str, Any]) -> list[str]:
    found = doc.get("compare")
    if not isinstance(found, dict):
        return []
    rows = cast(list[Mapping[str, Any]], cast(dict[str, Any], found).get("rows") or [])
    return [f"compare: {counted(rows, 'change', CHANGES) or 'no rows compared'}"]


class Refused(Exception):
    """The server refuses a call before any child starts. Its text says why."""


def positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number above 0, got {text!r}") from None
    if not (0 < value < float("inf")):
        raise argparse.ArgumentTypeError(f"expected a number above 0, got {text!r}")
    return value


CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PART_BYTES = 255  # the longest name one folder holds, on the file systems Linux uses


def inside(value: str, root: Path) -> Path:
    """A path argument as the child gets it: absolute, and inside the root once resolved.

    A relative value is read against the root, the coding agent's own working folder, never
    against the config's folder. The child gets it absolute, with every folder above its last
    part resolved, so neither its own working folder nor a link swapped higher up can change
    what it names; the last part stays as given, so a linked target stays a link, which show
    refuses to open as the CLI does.
    """
    if CONTROL.search(value) is not None:
        raise Refused(f"{value!r}: a path may not hold a control character")
    if value.startswith(("-", "~")):
        raise Refused(f"{value!r}: a path may not start with - or ~; write ./{value}")
    if len(value) > 1024 or any(
        len(part.encode("utf-8", "surrogateescape")) > PART_BYTES for part in Path(value).parts
    ):
        raise Refused(f"{value[:80]!r}...: the path is too long")
    joined = root / value  # an absolute value is read as it is
    real = Path(os.path.realpath(joined))
    if not real.is_relative_to(root):  # by parts, so /root-other is not inside /root
        raise Refused(f"{value!r}: the path is outside the project folder")
    # Every folder above the last part resolved now, so a link higher up swapped later cannot
    # move it; the last part as given, so a linked target stays a link, which show refuses.
    return Path(os.path.realpath(joined.parent)) / joined.name


def settings_from(argv: Sequence[str] | None) -> Settings:
    parser = argparse.ArgumentParser(
        prog="scrimmage-mcp",
        description="The Scrimmage MCP server, over stdio, for a coding agent.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, help="the scrimmage.toml every tool reads")
    parser.add_argument("--root", help="the project folder; the working folder by default")
    parser.add_argument(
        "--max-session-usd",
        type=positive,
        default=10.0,
        help="the most this server's calls may spend in all, in dollars (default 10)",
    )
    parser.add_argument(
        "--allow-no-budget",
        action="store_true",
        help='run test and record on a file with max_cost_usd = "none"',
    )
    parser.add_argument(
        "--allow-probe", action="store_true", help="let init run a function with --probe"
    )
    parser.add_argument(
        "--call-timeout",
        type=positive,
        default=1800.0,
        help="stop a command that runs longer than this, in seconds (default 1800)",
    )
    args = parser.parse_args(argv)
    given = os.path.abspath(args.root) if args.root is not None else os.environ.get("PWD", "")
    root = Path(os.path.realpath(args.root if args.root is not None else os.getcwd()))
    if root == Path(root.anchor) or not root.is_dir():
        parser.error(f"--root: {root} is not a project folder")
    config = Path(os.path.realpath(root / args.config))
    if not config.is_relative_to(root):
        parser.error(f"--config: {args.config} is not inside {root}")
    # The root as the person spelled it, such as through a link, names the same folder.
    same = given and given != str(root) and os.path.realpath(given) == str(root)
    return Settings(
        root,
        config,
        args.max_session_usd,
        args.allow_no_budget,
        args.allow_probe,
        args.call_timeout,
        (given,) if same else (),
    )


@dataclass
class Child:
    """One running scrimmage command, from the moment it exists."""

    proc: anyio.abc.Process
    nonce: str
    started: float
    command: str = ""
    stopped: str | None = None  # why the server stopped it, if it did
    swept: int = 0  # processes the sweep killed after it
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    err_bytes: int = 0  # all it wrote to standard error, of which err keeps the tail
    too_large: bool = False
    done: anyio.Event = field(default_factory=anyio.Event)  # both pipes closed, and it exited
    wake: anyio.Event = field(default_factory=anyio.Event)  # done, or its output is too large

    @property
    def pid(self) -> int:
        return self.proc.pid


@dataclass
class Ran:
    """What one child left: its exit code, and its document if it printed one."""

    command: str
    code: int | None
    doc: dict[str, Any] | None
    problem: str | None  # why there is no document
    tail: str  # the last line of its standard error
    seconds: float
    err_bytes: int = 0
    stopped: str | None = None
    swept: int = 0


# What a tool call reports progress with: progress, total, message.
Report = Callable[[float, float | None, str | None], Awaitable[None]]
GUARD = "scrimmage/guard"  # the _meta key of what the spending guards checked
BUSY = (
    "a test, record or probe is already running in this server, and it runs one at a time; "
    "wait for its result, then call again"
)


def checked_guard(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a check document says a spend would run under: the file's cap, its entry point
    and the labels under real."""
    found = None if doc is None else doc.get("summary")
    if not isinstance(found, dict):
        return {}
    summary = cast(dict[str, Any], found)
    assert doc is not None
    return {
        "file_max_cost_usd": summary.get("budget_usd"),
        "entry": summary.get("entry"),
        "real": summary.get("real"),
        "prices": summary.get("prices"),
        "checked_by": {"command": "check", "exit": doc.get("exit"), "status": doc.get("status")},
    }


def is_error(code: int) -> bool:
    return code in (MISTAKE, INFRA)


def text(block: str) -> types.TextContent:
    return types.TextContent(type="text", text=block)


class Scrimmage:
    """The server's state: its settings, its children, and what its calls spent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.children: dict[int, Child] = {}
        self.slots = anyio.Semaphore(MAX_CHILDREN)
        self.group: anyio.abc.TaskGroup | None = None
        self.validators = {
            name: jsonschema.Draft202012Validator(input_schema(name, settings.allow_probe))
            for name in COMMANDS
        }
        home = os.path.expanduser("~")
        self.paths = path_rule(
            [str(settings.root), *settings.aliases], sorted({home, os.path.realpath(home)})
        )
        self.spent = 0.0  # what this server's calls have spent, as they are counted
        self.spending = False  # a test, record or probe holds it from its check to its count
        self.calls = 0  # tool calls in flight
        self.shutting_down = False  # a shutdown signal or an escaped error: start nothing more

    def tools(self) -> list[types.Tool]:
        probe = self.settings.allow_probe
        return [
            types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                input_schema=input_schema(name, probe),
                output_schema=OUTPUT_SCHEMAS[name],
                annotations=hints(name, probe),
            )
            for name in COMMANDS
        ]

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(tools=self.tools())

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        self.calls += 1  # the exit after a shutdown waits for every call to have ended
        try:
            arguments = self.checked(params.name, params.arguments or {})
            # A no-op when the call carries no progress token.
            return await self.command(params.name, arguments, ctx.session.report_progress)
        except Refused as exc:
            return types.CallToolResult(content=[text(str(exc))], is_error=True)
        finally:
            self.calls -= 1

    def checked(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The arguments, if they fit the tool's input schema; else a refusal naming one."""
        validator = self.validators.get(name)
        if validator is None:
            raise Refused(f"no tool is named {name!r}; the tools are {', '.join(COMMANDS)}")
        # The overload with a second schema argument, deprecated, has no types.
        errors: list[ValidationError] = list(validator.iter_errors(arguments))  # pyright: ignore[reportUnknownMemberType]
        if errors:
            error = min(errors, key=lambda e: len(cast(Sequence[object], e.absolute_path)))
            path = cast(Sequence[object], error.absolute_path)
            where = ".".join(str(part) for part in path) or "arguments"
            raise Refused(f"{where}: {error.message}")
        # A schema pattern is a search, and its $ matches before a final newline.
        if "run" in arguments and RUN_ID.fullmatch(arguments["run"]) is None:
            raise Refused(f"run: {arguments['run']!r} is not a run id such as run-3f9a1c40b2e1")
        if "probe" in arguments and PROBE.fullmatch(arguments["probe"]) is None:
            raise Refused(f"probe: {arguments['probe']!r} is not module:function")
        return arguments

    def path(self, value: str) -> str:
        return str(inside(value, self.settings.root))

    def argv(self, name: str, arguments: Mapping[str, Any]) -> list[str]:
        """The child's command line after the command's name. Every value is a --flag=value or
        an absolute path, so none can become a flag."""
        config = str(self.settings.config)
        if name == "check":
            return [config]
        if name == "test":
            args = [config, f"--max-cost-usd={float(arguments['max_cost_usd'])!r}"]
            if "compare" in arguments:
                args.append(f"--compare={self.path(arguments['compare'])}")
            return args
        if name == "record":
            args = [config, f"--max-cost-usd={float(arguments['max_cost_usd'])!r}"]
            args += [f"--test={test}" for test in arguments.get("tests", [])]
            args += [f"--refresh={label}" for label in arguments.get("refresh", [])]
            if "runs" in arguments:
                args.append(f"--runs={arguments['runs']}")
            return args
        if name == "init":
            args = [self.path(arguments["folder"])] if "folder" in arguments else []
            args.append(f"--out={config}")
            if "probe" in arguments:
                args.append(f"--probe={arguments['probe']}")
            return args
        args = [self.path(arguments["target"])] if "target" in arguments else []
        if "run" in arguments:
            args.append(f"--run={arguments['run']}")
        if "compare" in arguments:
            args.append(f"--compare={self.path(arguments['compare'])}")
        return args

    async def command(
        self, name: str, arguments: dict[str, Any], report: Report | None = None
    ) -> types.CallToolResult:
        args = self.argv(name, arguments)
        if name not in ("test", "record") and not (name == "init" and "probe" in arguments):
            ran = await self.run(name, args, report=report)
            return self.result(ran, checked_guard(ran.doc) if name == "check" else {})
        # One spend at a time, held from the check to the end of the stop and the count, so a
        # cancelled call's runs are gone before the next spend can start.
        if self.spending:
            raise Refused(BUSY)
        self.spending = True
        try:
            if name == "init":
                ran = await self.run(name, args, report=report)
                return self.result(ran, {})
            return await self.spend(name, args, arguments, report)
        finally:
            self.spending = False

    async def spend(
        self, name: str, args: list[str], arguments: Mapping[str, Any], report: Report | None
    ) -> types.CallToolResult:
        """test or record, after the checks a spend needs. The cap the person approved, the
        max_cost_usd argument, goes to the child, whose ledger then holds it whatever the
        file says by the time the child reads it."""
        usd = float(arguments["max_cost_usd"])
        ceiling = self.settings.max_session_usd
        if self.spent + usd > ceiling:
            raise Refused(
                f"this call may spend up to ${usd:g}, and this server has spent "
                f"{spent_text(self.spent)} of its ${ceiling:g} ceiling for the session; call "
                "with a lower max_cost_usd, or restart the server with a higher "
                "--max-session-usd"
            )
        # --runs under the argument's cap, as the record would run: a file with no budget is not
        # refused for N above 1 when the argument caps it.
        runs = [f"--runs={arguments['runs']}"] if "runs" in arguments else []
        check_args = [str(self.settings.config), *runs, f"--max-cost-usd={usd!r}"]
        check = await self.run("check", check_args, limit=CHECK_LIMIT_S)
        if check.doc is None or is_error(int(check.doc["exit"])):
            return self.result(check, {}, first=f"{name}: check found problems; nothing was run")
        guard = checked_guard(check.doc)
        file_cap = guard["file_max_cost_usd"]
        if file_cap is None and not self.settings.allow_no_budget:
            raise Refused(
                'scrimmage.toml has max_cost_usd = "none", and this server runs test and '
                "record only with a cost cap in the file. Set max_cost_usd in [agent], or "
                "start the server with --allow-no-budget"
            )
        if file_cap is not None and file_cap > usd:
            raise Refused(
                f"the file's cost cap is ${file_cap:g}, above the ${usd:g} this call allows; "
                f"lower max_cost_usd in the file, or call again with max_cost_usd of at least "
                f"${file_cap:g}, which the person approves"
            )
        guard["max_cost_usd"] = usd
        ran = await self.run(name, args, spend=usd, report=report, cap=usd)
        # The file may have changed since the check: the child's cap is still at most usd.
        expected = usd if file_cap is None else min(file_cap, usd)
        used = None if ran.doc is None else ran.doc.get("budget_usd")
        extra = [] if used in (None, expected) else [
            f"the cost cap used, {cap_text(used)}, is not the {cap_text(expected)} the check "
            "found: scrimmage.toml changed after the check"
        ]  # fmt: skip
        return self.result(ran, guard, extra=extra)

    def count(self, ran: Ran, usd: float) -> None:
        """Add what a spending call may have spent to the session. A stopped call's cost_usd
        leaves out the calls still held in flight, so it counts at least its argument, and a
        call with no document counts all of it."""
        doc = ran.doc
        if doc is None:
            self.spent += usd
            return
        cost = float(doc.get("cost_usd") or 0.0)
        stopped = doc.get("status") == "infra" or doc.get("stopped_by") is not None
        self.spent += max(cost, usd) if stopped else cost

    def result(
        self,
        ran: Ran,
        guard: Mapping[str, Any],
        *,
        first: str | None = None,
        extra: Sequence[str] = (),
    ) -> types.CallToolResult:
        ceiling = self.settings.max_session_usd
        session = f"session: spent {spent_text(self.spent)} of ${ceiling:g}"
        meta = {
            GUARD: {
                "max_cost_usd": guard.get("max_cost_usd"),
                "file_max_cost_usd": guard.get("file_max_cost_usd"),
                "entry": guard.get("entry"),
                "real": guard.get("real"),
                "prices": guard.get("prices"),
                "session_spent_usd": round(self.spent, 8),
                "session_cap_usd": ceiling,
                **({"checked_by": guard["checked_by"]} if "checked_by" in guard else {}),
            }
        }
        heading = [first] if first is not None else []
        if ran.stopped is not None:
            extra = [*extra, ran.stopped]
        if ran.doc is None:
            line = f"{ran.command}: the command printed no document (exit {ran.code}): "
            said = "\n".join(
                [*heading, f"{line}{ran.problem}{self.tail_text(ran)}", *extra, session]
            )
            return types.CallToolResult(content=[text(said)], is_error=True, _meta=meta)
        doc = forwarded(ran.doc, self.paths)
        lines = [*heading, *summary_lines(doc, guard), *extra, session]
        return types.CallToolResult(
            content=[text("\n".join(lines)), text(json.dumps(doc, ensure_ascii=False))],
            structured_content=doc,
            is_error=first is not None or ran.stopped is not None or is_error(int(doc["exit"])),
            _meta=meta,
        )

    def tail(self, ran: Ran) -> str:
        """The last line the child wrote to standard error, as safe to show as a message."""
        return redact(self.paths(ran.tail))[:TAIL_CHARS]

    def tail_text(self, ran: Ran) -> str:
        return f"; its last line on standard error: {self.tail(ran)}" if ran.tail else ""

    def log(self, ran: Ran) -> None:
        """One line per call on the server's own standard error, which the client keeps in its
        local log. A cancelled call's document reaches only this line."""
        doc = ran.doc or {}
        line = (
            f"scrimmage-mcp: {ran.command}: exit {ran.code} in {ran.seconds:.1f} s, "
            f"cost_usd {doc.get('cost_usd', 0.0)}, {ran.err_bytes} bytes on standard error"
        )
        if ran.stopped is not None:
            line += f", {ran.stopped}, records {doc.get('records')}"
        line += f", {ran.swept} left over and killed by the sweep"
        if ran.doc is None:
            line += f", no document: {ran.problem}{self.tail_text(ran)}"
        else:  # a cancelled call's document reaches only here, so its status and result do
            line += f", status {doc.get('status')}"
        print(line, file=sys.stderr, flush=True)

    async def run(
        self,
        command: str,
        args: list[str],
        *,
        spend: float | None = None,
        report: Report | None = None,
        cap: float | None = None,
        limit: float | None = None,
    ) -> Ran:
        """Run one scrimmage command as a child and read its one document.

        spend: the most a spending call may cost, added to the session when it ends, however
        it ends. report: sends progress while it runs. limit: the server's own time limit.
        """
        limit = self.settings.call_timeout if limit is None else limit
        async with self.slots:
            started: list[Child] = []  # filled in the same shielded step that starts it
            try:
                await self.start(command, args, started)
                (child,) = started
                reason = await self.wait(child, limit, report, cap)
                if reason is not None:  # too large, or past the time limit: it is stopped
                    child.stopped = reason
                    await self.stop(child)
            except BaseException:  # a cancelled call: the child is stopped before it goes on
                for child in started:
                    child.stopped = "the call was cancelled"
                    await self.stop(child)
                    self.ended(self.read(command, child), spend)
                raise
            finally:
                for child in started:
                    self.children.pop(child.pid, None)
            (child,) = started
            if child.stopped is None:
                child.swept = self.sweep(child)  # finds nothing when the child's stop worked
            ran = self.read(command, child)
            self.ended(ran, spend)
            return ran

    async def wait(
        self, child: Child, limit: float, report: Report | None, cap: float | None
    ) -> str | None:
        """Wait for the child, sending progress every few seconds. The child prints nothing
        until it ends, so the time it has run is all progress can say. Returns why it must be
        stopped, if it must."""
        deadline = child.started + limit
        while not child.wake.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return f"stopped after the server's {limit:g} s limit"
            with anyio.move_on_after(min(PROGRESS_EVERY_S, left)):
                await child.wake.wait()
            if not child.wake.is_set() and report is not None:
                ran_for = time.monotonic() - child.started
                said = f"{child.command} running for {ran_for:.0f} s"
                await report(ran_for, None, said if cap is None else f"{said}; cost cap ${cap:g}")
        if not child.done.is_set():
            return f"its document was over {DOC_CAP // 2**20} MB"
        return None

    def ended(self, ran: Ran, spend: float | None) -> None:
        if spend is not None:
            self.count(ran, spend)
        self.log(ran)

    async def start(self, command: str, args: list[str], started: list[Child]) -> None:
        """Start the child and track it in one shielded step, so a cancel that lands while it
        starts cannot leave a child no one stops: the caller's list holds it before anything
        can raise, even the shielded scope's own exit, where anyio delivers a pending cancel."""
        assert self.group is not None
        if self.shutting_down:
            raise Refused("the server is shutting down; nothing more is started")
        nonce = secrets.token_hex(16)
        with anyio.CancelScope(shield=True):
            proc = await anyio.open_process(
                [sys.executable, "-P", "-m", "scrimmage", command, *args, "--json"],
                stdin=subprocess.DEVNULL,  # the server's standard input is the protocol stream
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.settings.config.parent,
                env={**os.environ, CALL_VARIABLE: nonce},
                start_new_session=True,  # only the server signals it
            )
            child = Child(proc, nonce, time.monotonic(), command)
            self.children[proc.pid] = child
            started.append(child)
            self.group.start_soon(self.supervise, child)
            if self.shutting_down:  # a shutdown came while it started: it missed that SIGTERM
                self.signal(child, signal.SIGTERM)

    async def supervise(self, child: Child) -> None:
        """Read both pipes at once, so a child that fills one never blocks on it, then wait for
        its exit. Runs in the server's own task group, which a cancelled call does not reach."""
        # Shielded: an error that tears down the server's task group must not mark a child
        # done while it still runs. It ends when the child does, which a stop ensures.
        with anyio.CancelScope(shield=True):
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(self.read_out, child)
                    group.start_soon(self.read_err, child)
                await child.proc.wait()
            finally:
                child.done.set()
                child.wake.set()

    async def read_out(self, child: Child) -> None:
        stream = child.proc.stdout
        assert stream is not None
        async for chunk in stream:
            if child.too_large:
                continue  # drained, so the child is never stuck on a full pipe
            child.out += chunk
            if len(child.out) > DOC_CAP:
                child.too_large = True
                child.out.clear()
                child.wake.set()

    async def read_err(self, child: Child) -> None:
        stream = child.proc.stderr
        assert stream is not None
        async for chunk in stream:
            child.err += chunk
            child.err_bytes += len(chunk)
            del child.err[:-STDERR_KEPT]

    async def stop(self, child: Child) -> None:
        """Stop a child as Scrimmage's own stop does, for a cancel, a time limit or shutdown.

        SIGTERM first: the child stops every run's group at once, SIGKILLs what is left after
        its own grace, and prints its document. Then SIGKILL to the child's group if it is still
        alive, then a sweep for every process that holds the call's nonce. A run's own program
        gets SIGKILL from Linux when its runner dies, but the processes it starts do not inherit
        that, and may be in sessions of their own, which no group signal reaches.
        """
        self.signal(child, signal.SIGTERM)  # before any await, so no cancel can come between
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(STOP_GRACE_S):
                await child.done.wait()
            if not child.done.is_set():
                self.signal_group(child, signal.SIGKILL)
                with anyio.move_on_after(KILL_REAP_S):
                    await child.done.wait()
            child.swept = self.sweep(child)

    def sweep(self, child: Child) -> int:
        """SIGKILL every process that started with this call's nonce in its environment.

        /proc/PID/environ is the environment a process started with, so one that unset the
        variable later is still found; only one started with a fresh environment is not. A
        system with no /proc has no sweep, and relies on Scrimmage's own stop alone.
        """
        mark = f"{CALL_VARIABLE}={child.nonce}".encode()
        killed = 0
        with contextlib.suppress(OSError):
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit() or int(entry.name) == os.getpid():
                    continue
                try:
                    with open(f"/proc/{entry.name}/environ", "rb") as file:
                        holds = mark in file.read().split(b"\0")
                    if holds:
                        os.kill(int(entry.name), signal.SIGKILL)
                        killed += 1
                except OSError:  # gone, or not ours to read
                    continue
        return killed

    def terminate_all(self) -> None:
        """SIGTERM to every running child, awaiting nothing: for a signal to the server, an
        error that escapes its loop, and its exit."""
        for child in list(self.children.values()):
            self.signal(child, signal.SIGTERM)

    def exit_once_stopped(self, code: int) -> None:
        """End the process once every call's stop has finished, from a thread of its own.

        The SDK reads the protocol stream in a worker thread, which no cancel can interrupt,
        so after a shutdown signal or an error the server would stay up until the client
        closed its standard input. Each child is untracked only once its stop is done and
        its line is written, so waiting for none to be left loses no stop and no line.
        os._exit skips main's finally and the atexit function; both stay, as a net for an exit
        that does not come this way.
        """

        def wait_then_exit() -> None:
            deadline = time.monotonic() + STOP_GRACE_S + KILL_REAP_S + EXIT_SLACK_S
            while (self.children or self.calls) and time.monotonic() < deadline:
                time.sleep(0.05)
            sys.stderr.flush()
            os._exit(code)

        threading.Thread(target=wait_then_exit, name="scrimmage-mcp-exit", daemon=True).start()

    def signal(self, child: Child, signum: int) -> None:
        if child.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child.pid, signum)

    def signal_group(self, child: Child, signum: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signum)

    def read(self, command: str, child: Child) -> Ran:
        seconds = time.monotonic() - child.started
        code = child.proc.returncode
        lines = child.err.decode("utf-8", "replace").splitlines()
        tail = next((line.strip() for line in reversed(lines) if line.strip()), "")
        doc: object = None
        problem: str | None = None
        if child.too_large:
            problem = f"its document was over {DOC_CAP // 2**20} MB"
        else:
            try:
                doc = json.loads(child.out)
            except ValueError:
                problem = "its standard output is not JSON"
        if problem is None and (
            not isinstance(doc, dict) or cast(dict[str, Any], doc).get("exit") != code
        ):
            problem = "its standard output is not a document whose exit is the child's"
        found = cast(dict[str, Any], doc) if problem is None else None
        return Ran(
            command,
            code,
            found,
            problem,
            tail,
            seconds,
            child.err_bytes,
            child.stopped,
            child.swept,
        )


def without_telemetry(server: Server[Any]) -> None:
    """mcp puts OpenTelemetry middleware on every server. With an OpenTelemetry SDK and an
    exporter set up in the team's environment, it would send this server's spans off the
    machine, so it is taken out, matched by module so no private name is imported."""
    server.middleware[:] = [
        m for m in server.middleware if type(m).__module__ != "mcp.server._otel"
    ]


def build(state: Scrimmage) -> Server[Any]:
    server: Server[Any] = Server(
        "scrimmage",
        version=versions.installed(),
        instructions=INSTRUCTIONS,
        on_list_tools=state.list_tools,
        on_call_tool=state.call_tool,
    )
    without_telemetry(server)
    return server


SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)  # SIGHUP: a closed terminal


async def serve(state: Scrimmage) -> None:
    server = build(state)
    loop = asyncio.get_running_loop()
    finished = False

    def failed(said: str) -> None:
        """An error that escapes the loop: every child gets SIGTERM, awaiting nothing, and the
        process ends once their stops are done, whatever the protocol stream's reader does."""
        state.shutting_down = True  # first: a child that starts now is signalled
        state.terminate_all()
        print(f"scrimmage-mcp: stopped on an error: {said}", file=sys.stderr, flush=True)
        state.exit_once_stopped(1)

    async def watch() -> None:
        """Runs until the server's task group is torn down; if that is not a normal end or a
        shutdown signal, a task of the server failed."""
        try:
            await anyio.sleep_forever()
        finally:
            if not finished and not serving.cancel_called:
                failed("a task of the server failed")

    serving = anyio.CancelScope()
    async with anyio.create_task_group() as group:
        state.group = group
        group.start_soon(watch)
        with serving:

            def shut_down(signum: int, frame: object) -> None:
                """Every child gets SIGTERM here, in the handler, with no turn of the event
                loop first. Then serving ends, and each call's stop finishes the job."""
                del frame
                state.shutting_down = True  # first: a child that starts now is signalled
                state.terminate_all()
                loop.call_soon_threadsafe(serving.cancel)
                state.exit_once_stopped(128 + signum)

            for signum in SHUTDOWN_SIGNALS:
                signal.signal(signum, shut_down)
            async with stdio_server() as (read_stream, write_stream):
                options = server.create_initialization_options()
                try:
                    await server.run(read_stream, write_stream, options)
                except Exception as exc:  # before stdio_server waits on its reader thread
                    failed(repr(exc))
                    raise
        finished = True
        group.cancel_scope.cancel()  # ends watch; each child's reader is shielded


def main(argv: Sequence[str] | None = None) -> None:
    state = Scrimmage(settings_from(argv))
    # The last nets: an error that escapes the loop, and a plain exit, still signal every
    # child, awaiting nothing.
    atexit.register(state.terminate_all)
    try:
        anyio.run(serve, state)
    finally:
        state.terminate_all()
