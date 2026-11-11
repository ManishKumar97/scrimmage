"""The Scrimmage MCP server: a coding agent runs scrimmage check, init, record, test and show.

Each tool call starts `python -P -m scrimmage_core <command> ... --json` as a child process on
this server's own Python, and returns the child's one JSON document. A command never runs in
this process, so its signal handlers, its imports and its working folder stay its own.
The server speaks MCP over stdio only: the client starts it, and nothing listens on a socket.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any, cast

INSTALL = 'scrimmage-mcp needs the mcp extra; install it with: pip install "scrimmage-core[mcp]"'

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
# How long a stopped child has to print its document: the runner's own grace to stop its
# runs, KILL_GRACE_S, plus 10 s for the report on a loaded machine. Written out, as this
# module never imports the runner.
STOP_GRACE_S = 15.0
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
    text = resources.files("scrimmage_core").joinpath("schemas/cli.v1.json").read_text("utf-8")
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
    root = Path(os.path.realpath(args.root if args.root is not None else os.getcwd()))
    if root == Path(root.anchor) or not root.is_dir():
        parser.error(f"--root: {root} is not a project folder")
    config = Path(os.path.realpath(root / args.config))
    if not config.is_relative_to(root):
        parser.error(f"--config: {args.config} is not inside {root}")
    return Settings(
        root,
        config,
        args.max_session_usd,
        args.allow_no_budget,
        args.allow_probe,
        args.call_timeout,
    )


def scrimmage_version() -> str:
    try:
        return metadata.version("scrimmage-core")
    except metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class Child:
    """One running scrimmage command, from the moment it exists."""

    proc: anyio.abc.Process
    nonce: str
    started: float
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
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
        del ctx
        try:
            arguments = self.checked(params.name, params.arguments or {})
            return await self.command(params.name, arguments)
        except Refused as exc:
            return types.CallToolResult(content=[text(str(exc))], is_error=True)

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

    def argv(self, name: str, arguments: Mapping[str, Any]) -> list[str]:
        """The child's command line after the command's name."""
        config = str(self.settings.config)
        if name == "check":
            return [config]
        if name == "test":
            args = [config, f"--max-cost-usd={float(arguments['max_cost_usd'])!r}"]
            if "compare" in arguments:
                args.append(f"--compare={arguments['compare']}")
            return args
        if name == "record":
            args = [config, f"--max-cost-usd={float(arguments['max_cost_usd'])!r}"]
            args += [f"--test={test}" for test in arguments.get("tests", [])]
            args += [f"--refresh={label}" for label in arguments.get("refresh", [])]
            if "runs" in arguments:
                args.append(f"--runs={arguments['runs']}")
            return args
        if name == "init":
            args = [arguments["folder"]] if "folder" in arguments else []
            args.append(f"--out={config}")
            if "probe" in arguments:
                args.append(f"--probe={arguments['probe']}")
            return args
        args = [arguments["target"]] if "target" in arguments else []
        if "run" in arguments:
            args.append(f"--run={arguments['run']}")
        if "compare" in arguments:
            args.append(f"--compare={arguments['compare']}")
        return args

    async def command(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        ran = await self.run(name, self.argv(name, arguments))
        return self.result(ran)

    def result(self, ran: Ran) -> types.CallToolResult:
        if ran.doc is None:
            line = f"{ran.command}: the command printed no document (exit {ran.code}): "
            return types.CallToolResult(
                content=[text(line + (ran.problem or "") + self.tail_text(ran))], is_error=True
            )
        doc = ran.doc
        summary = f"{ran.command}: {doc['status']} (exit {doc['exit']})"
        return types.CallToolResult(
            content=[text(summary), text(json.dumps(doc, indent=1))],
            structured_content=doc,
            is_error=is_error(int(doc["exit"])),
        )

    def tail_text(self, ran: Ran) -> str:
        return f"; its last line on standard error: {ran.tail}" if ran.tail else ""

    async def run(self, command: str, args: list[str]) -> Ran:
        """Run one scrimmage command as a child and read its one document."""
        async with self.slots:
            child = await self.start(command, args)
            try:
                await child.wake.wait()
                if not child.done.is_set():  # its output is too large: it is stopped
                    await self.stop(child)
            except BaseException:  # a cancelled call: the child is stopped before it goes on
                await self.stop(child)
                raise
            finally:
                self.children.pop(child.pid, None)
            return self.read(command, child)

    async def start(self, command: str, args: list[str]) -> Child:
        """Start the child and track it in one shielded step, so a cancel that lands while it
        starts cannot leave a child no one stops."""
        assert self.group is not None
        nonce = secrets.token_hex(16)
        with anyio.CancelScope(shield=True):
            proc = await anyio.open_process(
                [sys.executable, "-P", "-m", "scrimmage_core", command, *args, "--json"],
                stdin=subprocess.DEVNULL,  # the server's standard input is the protocol stream
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.settings.config.parent,
                env={**os.environ, CALL_VARIABLE: nonce},
                start_new_session=True,  # only the server signals it
            )
            child = Child(proc, nonce, time.monotonic())
            self.children[proc.pid] = child
            self.group.start_soon(self.supervise, child)
            return child
        raise AssertionError("a shielded scope no one cancels was cancelled")  # pragma: no cover

    async def supervise(self, child: Child) -> None:
        """Read both pipes at once, so a child that fills one never blocks on it, then wait for
        its exit. Runs in the server's own task group, which a cancelled call does not reach."""
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
            del child.err[:-STDERR_KEPT]

    async def stop(self, child: Child) -> None:
        """Stop a child as Scrimmage's own stop does: SIGTERM, which stops its runs and prints
        its document, then SIGKILL to its group if it is still alive after the grace."""
        self.signal(child, signal.SIGTERM)  # before any await, so no cancel can come between
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(STOP_GRACE_S):
                await child.done.wait()
            if not child.done.is_set():
                self.signal_group(child, signal.SIGKILL)
                with anyio.move_on_after(KILL_REAP_S):
                    await child.done.wait()

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
        if child.too_large:
            problem = f"its document was over {DOC_CAP // 2**20} MB"
            return Ran(command, code, None, problem, tail, seconds)
        try:
            doc = json.loads(child.out)
        except ValueError:
            return Ran(command, code, None, "its standard output is not JSON", tail, seconds)
        if not isinstance(doc, dict) or cast(dict[str, Any], doc).get("exit") != code:
            problem = "its standard output is not a document whose exit is the child's"
            return Ran(command, code, None, problem, tail, seconds)
        return Ran(command, code, cast(dict[str, Any], doc), None, tail, seconds)


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
        version=scrimmage_version(),
        instructions=INSTRUCTIONS,
        on_list_tools=state.list_tools,
        on_call_tool=state.call_tool,
    )
    without_telemetry(server)
    return server


async def serve(state: Scrimmage) -> None:
    server = build(state)
    async with anyio.create_task_group() as group:
        state.group = group
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: Sequence[str] | None = None) -> None:
    state = Scrimmage(settings_from(argv))
    anyio.run(serve, state)
