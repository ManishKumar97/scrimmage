"""`faultline run`: start the proxy, run the agent through it, exit when the agent does.

    faultline run [--fault SPEC]... [--expect TEXT] [--upstream URL]
                  [--base-url-env NAME]... [--trace DIR] -- CMD...

Model SDKs read their base URL from an environment variable. The agent gets the
proxy's URL in those variables, so its model calls go through the proxy with no
change to its code. Each variable gets its own route on the proxy, sent on to its
own upstream: the URL the agent would have used. So an Anthropic call, and its key,
never go to the OpenAI upstream.

A variable is pointed at the proxy only when its upstream is known: from
--upstream NAME=URL, or from the variable's value before the run.

Exit code: the agent's own, or 78 when a requested fault was never applied, such
as a tool fault on a tool the agent never called. The verdict is then not_applied.

Live tools. A run must not reach a tool that changes the world by default, so without
--allow-live-tools the run is a preview: every MCP server under faultline mcp-wrap
lists its tools, every tool call is blocked, and the proxy holds every model call.
As soon as each server that started has listed its tools and the agent asks the
model for its first step (or a short grace time passes), faultline stops the agent,
prints every tool by server, and exits 2. Tools in the agent's own code never pass
through faultline, so they cannot be listed.

`execute()` is one run, for `faultline test` too: every run goes the same way.
"""

import argparse
import asyncio
import contextlib
import functools
import os
import secrets
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, TextIO

from faultline.cost import Budget
from faultline.faults import (
    Fault,
    FaultError,
    ModelFault,
    known,
    parse_all,
    span_files,
)
from faultline.model import Run
from faultline.proxy.context import MAX_CONTENT, RunContext, publish, withdraw
from faultline.proxy.listing import ServerTools, read_all
from faultline.proxy.record import DEFAULT_MAX_CONTENT, Recorder
from faultline.relay import RELAYED, Relay, tied_to_parent
from faultline.runs import read_runs_many
from faultline.verdict import judge, line, never_applied, not_applied_line

if TYPE_CHECKING:
    from faultline.proxy.model import ModelProxy

# The exit code when a requested fault was never applied: the run tested nothing
# that was asked. 78 is EX_CONFIG from sysexits.h, a configuration error.
NOT_APPLIED = 78

# The variables the OpenAI and Anthropic SDKs read their base URL from.
BASE_URL_ENVS = ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")
# The exit code of a preview: the tools were listed, and nothing ran.
PREVIEW = 2
# Seconds a preview waits for the servers to list their tools.
LIST_GRACE = "FAULTLINE_LIST_GRACE"
DEFAULT_LIST_GRACE = 120.0  # an MCP server can take a minute to start on a busy machine
POLL = 0.1


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "run",
        help="run an agent with its model and tool calls through the proxy",
        description="Run CMD with its model calls through a local proxy.",
    )
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="SPEC",
        help="a fault to inject (repeatable): "
        f"{', '.join(known())}. A tool fault needs the MCP server started "
        "under faultline mcp-wrap",
    )
    parser.add_argument(
        "--expect",
        metavar="TEXT",
        help="text the answer must contain for the run to pass",
    )
    add_upstream_arguments(parser)
    add_live_tools_argument(parser)
    parser.add_argument(
        "--trace",
        type=Path,
        metavar="DIR",
        help="where the run's spans go (default: a new temporary directory)",
    )
    parser.add_argument(
        "--stale-from",
        type=Path,
        metavar="DIR",
        help="the trace directory of an earlier run, for tool:NAME:stale to serve "
        "the results that run recorded",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- CMD...")
    parser.set_defaults(handler=run)


def add_upstream_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags that say where the agent's model calls go. faultline test has them too."""
    parser.add_argument(
        "--upstream",
        action="append",
        default=[],
        metavar="[NAME=]URL",
        help="the model API base URL that variable NAME stands for (repeatable). "
        "A bare URL is for the first --base-url-env, else OPENAI_BASE_URL. "
        "Default: each variable's own value before the run",
    )
    parser.add_argument(
        "--base-url-env",
        action="append",
        default=[],
        metavar="NAME",
        help="another variable the agent reads its model base URL from (repeatable); "
        f"{' and '.join(BASE_URL_ENVS)} are always set",
    )
    parser.add_argument(
        "--max-content",
        type=int,
        metavar="CHARS",
        default=int(os.environ.get(MAX_CONTENT) or DEFAULT_MAX_CONTENT),
        help="the most characters of message or tool content one span records; "
        f"over it, content is cut and marked (default {DEFAULT_MAX_CONTENT}, "
        f"or {MAX_CONTENT})",
    )


def add_live_tools_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-live-tools",
        action="store_true",
        help="let tool calls reach their MCP servers for real. Without it, faultline "
        "lists the tools that would run, runs none, and exits 2",
    )


def command_of(args: argparse.Namespace) -> list[str]:
    return args.command[1:] if args.command[:1] == ["--"] else args.command


def run(args: argparse.Namespace) -> int:
    command = command_of(args)
    if not command:
        print("faultline run: give the agent's command after --", file=sys.stderr)
        return 2
    try:
        faults = parse_all(args.fault)
    except FaultError as exc:
        print(f"faultline run: {exc}", file=sys.stderr)
        return 2
    if args.stale_from is not None:
        if not args.stale_from.is_dir():
            print(
                f"faultline run: --stale-from {args.stale_from} is not a directory",
                file=sys.stderr,
            )
            return 2
        _, problems = span_files(args.stale_from)
        for problem in problems:
            print(
                f"faultline run: --stale-from {args.stale_from}: cannot read {problem}",
                file=sys.stderr,
            )
        if problems:
            return 2
    upstreams = routed_upstreams(args, "faultline run")
    if upstreams is None or not proxy_ready("faultline run"):
        return 2
    trace = args.trace or Path(tempfile.mkdtemp(prefix="faultline-run-"))
    stale_from = args.stale_from.resolve() if args.stale_from else None
    if not args.allow_live_tools:
        outcome = execute(
            command,
            faults,
            upstreams,
            trace,
            args.max_content,
            stale_from,
            preview=True,
        )
        print(
            show_tools(outcome.servers, "faultline run", outcome.unrouted),
            file=sys.stderr,
        )
        return PREVIEW
    outcome = execute(command, faults, upstreams, trace, args.max_content, stale_from)
    for spec, reason in outcome.missed:
        print(
            f"faultline run: fault {spec} was never applied ({reason})", file=sys.stderr
        )
    if outcome.missed:
        # The run did not test what was asked, so no passed or failed is honest.
        print(not_applied_line([spec for spec, _ in outcome.missed]), file=sys.stderr)
        return NOT_APPLIED
    verdict = judge(
        outcome.run,
        outcome.trace_id,
        outcome.code,
        args.expect,
        outcome.answer_unrecorded,
    )
    print(line(verdict), file=sys.stderr)
    return outcome.code


def routed_upstreams(
    args: argparse.Namespace, label: str
) -> dict[str, str | None] | None:
    """Each base URL variable and its upstream, or None after saying none is known."""
    # Names given on the command line come first: they say which variable this agent reads.
    names = list(dict.fromkeys([*args.base_url_env, *BASE_URL_ENVS]))
    upstreams = upstreams_of(args.upstream, names)
    if not any(upstreams.values()):
        print(
            f"{label}: no upstream. None of {', '.join(names)} is set; "
            "pass --upstream URL, the model API base URL the agent would use.",
            file=sys.stderr,
        )
        return None
    return upstreams


def proxy_ready(label: str) -> bool:
    """True when the proxy can load. Else says which extra to install."""
    try:
        import faultline.proxy.model  # noqa: F401  aiohttp loads only for a run
    except ImportError as exc:
        print(
            f"{label}: the proxy needs the proxy extra ({exc.name} is missing): "
            "pip install 'faultline[proxy]'",
            file=sys.stderr,
        )
        return False
    return True


def upstreams_of(flags: Sequence[str], names: list[str]) -> dict[str, str | None]:
    """Each base URL variable and its upstream, None when unknown. Adds named ones."""
    given: dict[str, str] = {}
    for flag in flags:
        name, sep, url = flag.partition("=")
        if sep and name.isidentifier():
            given[name] = url
            if name not in names:
                names.append(name)
        else:
            given[names[0]] = flag  # a bare URL, for the first variable
    return {name: given.get(name) or os.environ.get(name) or None for name in names}


@dataclass(frozen=True)
class Streams:
    """Where the agent's standard streams go. None keeps faultline's own.

    `log` takes faultline's own lines about the run, such as the proxy's address.
    """

    stdin: int | IO | None = None
    stdout: int | IO | None = None
    stderr: int | IO | None = None
    log: TextIO | None = None


@dataclass(frozen=True)
class Outcome:
    """One finished run of the agent, read back from its trace."""

    code: int  # the exit code a shell would give
    trace: Path
    trace_id: str
    run: Run | None  # None when no model call was recorded
    missed: list[tuple[str, str]]  # requested faults never applied, and why
    answer_unrecorded: bool  # the last reply was a stream the proxy could not read
    seconds: float
    interrupted: bool  # a stop signal reached faultline during the run
    servers: list[ServerTools]  # the MCP servers that ran, and the tools they listed
    # Base URL variables with no upstream: the agent's calls through them bypass
    # faultline.
    unrouted: list[str]


def execute(
    command: Sequence[str],
    faults: Sequence[Fault],
    upstreams: Mapping[str, str | None],
    trace: Path,
    max_content: int = DEFAULT_MAX_CONTENT,
    stale_from: Path | None = None,
    streams: Streams | None = None,
    budget: Budget | None = None,
    preview: bool = False,
) -> Outcome:
    """Run the agent once through the proxy and read the run back from its trace.

    With a budget, the proxy refuses model calls once it is spent. A preview blocks
    every tool call and holds every model call, and stops the agent once its MCP
    servers have listed their tools.
    Call proxy_ready() first: this loads the proxy.
    """
    from faultline.proxy.model import ModelProxy

    trace.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(
        trace.resolve() / "model.jsonl",
        secrets.token_hex(16),
        secrets.token_hex(8),
        max_content=max_content,
    )
    model_faults = [fault for fault in faults if isinstance(fault, ModelFault)]
    proxy = ModelProxy(upstreams, model_faults, recorder, budget, hold=preview)
    clock = time.monotonic()
    code, interrupted = asyncio.run(
        launch(
            proxy,
            recorder,
            command,
            [fault.spec for fault in faults],
            str(stale_from) if stale_from else None,
            streams,
            preview,
        )
    )
    seconds = time.monotonic() - clock
    # The verdict reads the run back from its trace, as Find reads any run.
    files = sorted(recorder.path.parent.glob("*.jsonl"))
    runs = [r for r in read_runs_many(files).runs if r.trace_id == recorder.trace_id]
    run_read = runs[0] if runs else None
    wrapped = any(f.name.startswith("mcp-") for f in files)
    return Outcome(
        code=code,
        trace=trace,
        trace_id=recorder.trace_id,
        run=run_read,
        missed=never_applied(faults, run_read, wrapped),
        answer_unrecorded=recorder.last_unparsed,
        seconds=seconds,
        interrupted=interrupted,
        servers=read_all(trace),
        unrouted=[name for name, url in upstreams.items() if not url],
    )


async def launch(
    proxy: "ModelProxy",
    recorder: Recorder,
    command: Sequence[str],
    faults: Sequence[str],
    stale_from: str | None = None,
    streams: Streams | None = None,
    preview: bool = False,
) -> tuple[int, bool]:
    """Run the command under the proxy. Returns the exit code a shell would give,
    and whether a stop signal reached faultline during the run."""
    streams = streams or Streams()
    log = streams.log or sys.stderr
    url = await proxy.start()
    env = dict(os.environ)
    routed = {name: up for name, up in proxy.upstreams_by_name().items() if up}
    env.update({name: proxy.url_for(name) for name in routed})
    run = RunContext(
        str(recorder.path.parent),
        recorder.trace_id,
        recorder.root_span_id,
        tuple(faults),
        recorder.max_content,
        stale_from,
        block_tools=preview,
    )
    # Two ways to find the run: variables for the agent, and a file for children
    # started with a cleaned environment, such as MCP servers.
    env.update(run.env())
    published = publish(run)
    for name, upstream in routed.items():
        print(f"faultline: {name}={proxy.url_for(name)} -> {upstream}", file=log)
    for name in proxy.upstreams_by_name().keys() - routed.keys():
        print(f"faultline: {name} not set, its calls do not go through", file=log)
    print(f"faultline: proxy {url}, spans in {recorder.path.parent}", file=log)
    log.flush()  # before the agent writes, which may be to the same file
    started = time.time_ns()
    code, interrupted = 1, False  # what the root records if faultline fails mid-run
    until = None
    if preview:
        print(
            "faultline: preview, no tool runs for real: listing the tools first",
            file=log,
        )
        trace = recorder.path.parent
        until = functools.partial(tools_listed, trace, proxy.called, list_grace())
    try:
        code, interrupted = await supervise(command, env, streams, until)
    finally:
        withdraw(published)
        # Whatever ended the run, the trace is finished: the last step and the root.
        await proxy.stop()  # after the agent, so its last calls still go through
        recorder.flush()
        recorder.root(started, time.time_ns(), command, code)
    return code, interrupted


async def supervise(
    command: Sequence[str],
    env: dict[str, str],
    streams: Streams | None = None,
    until: Callable[[], Awaitable[None]] | None = None,
) -> tuple[int, bool]:
    """Run the agent and pass signals on to it. Returns its exit code as a shell
    gives it, and whether a stop signal came during the run.

    When `until` returns before the agent exits, the agent gets SIGTERM, and SIGKILL
    after the grace period.
    """
    streams = streams or Streams()
    loop = asyncio.get_running_loop()
    relay = Relay()
    for number in RELAYED:
        loop.add_signal_handler(number, relay.receive, number)
    try:
        try:
            child = await asyncio.create_subprocess_exec(
                *tied_to_parent(command, "faultline run"),
                env=env,
                stdin=streams.stdin,
                stdout=streams.stdout,
                stderr=streams.stderr,
            )
        except OSError as exc:
            print(
                f"faultline run: cannot start {command[0]}: {exc}",
                file=streams.log or sys.stderr,
            )
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            return code, relay.signalled
        relay.attach(child)
        exited = asyncio.ensure_future(child.wait())
        if until is not None:
            stop = asyncio.ensure_future(until())
            await asyncio.wait({exited, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop.done() and not exited.done():
                relay.send(signal.SIGTERM)
            stop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop
        code = await exited
    finally:
        relay.close()
        for number in RELAYED:
            loop.remove_signal_handler(number)
    # Killed by signal N: 128 + N.
    return (128 - code if code < 0 else code), relay.signalled


def list_grace() -> float:
    try:
        return float(os.environ.get(LIST_GRACE) or DEFAULT_LIST_GRACE)
    except ValueError:
        return DEFAULT_LIST_GRACE


async def tools_listed(trace: Path, called: asyncio.Event, grace: float) -> None:
    """Return once the agent has asked the model for its first step and every MCP
    server that started has listed its tools. Return after `grace` seconds in any case.

    The first model call is the sign the agent has set up its tools: it sends their
    definitions with the call. A quiet spell is no such sign, as one server may start
    only after another has listed its tools, seconds later on a loaded machine.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    while loop.time() - started < grace:
        if called.is_set():
            servers = read_all(trace)
            if all(server.tools is not None for server in servers):
                return
        await asyncio.sleep(POLL)


def show_tools(
    servers: Sequence[ServerTools], label: str, unrouted: Sequence[str] = ()
) -> str:
    """What a preview found: every tool that would run for real, by server.

    It claims only what faultline sees: MCP calls under faultline mcp-wrap, and
    model calls through the proxy's routes.
    """
    lines = [
        (
            f"{label}: a preview. No MCP tool under faultline mcp-wrap ran: "
            "every call was blocked, and every model call was held."
        )
    ]
    if not servers:
        lines.append("No MCP server ran under faultline mcp-wrap.")
    else:
        lines.append("These MCP tools would run for real:")
    for server in servers:
        command = " ".join(server.command)
        named = f"{server.server} ({command})" if server.server else command
        lines.append(f"  {named}:")
        if server.tools is None:
            lines.append(
                "    (it never listed its tools before faultline stopped the agent)"
            )
        elif not server.tools:
            lines.append("    (no tools)")
        lines += [f"    {tool}" for tool in server.tools or []]
    lines.append(
        "Tools in the agent's own code (function tools) never pass through "
        "faultline, so they are not listed. Check them yourself."
    )
    for name in unrouted:
        lines.append(
            f"{name} was not set, so the agent's calls through it bypass faultline "
            "and were not held."
        )
    lines.append("Pass --allow-live-tools to run with these tools.")
    return "\n".join(lines)
