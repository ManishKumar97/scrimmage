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
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.faults import FaultError, ModelFault, known, parse_all
from faultline.proxy.record import FAULTS, ROOT_SPAN_ID, TRACE_DIR, TRACE_ID, Recorder
from faultline.relay import RELAYED, Relay
from faultline.runs import read_runs_many
from faultline.verdict import judge, line

if TYPE_CHECKING:
    from faultline.proxy.model import ModelProxy

# The variables the OpenAI and Anthropic SDKs read their base URL from.
BASE_URL_ENVS = ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")


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
        "--trace",
        type=Path,
        metavar="DIR",
        help="where the run's spans go (default: a new temporary directory)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- CMD...")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("faultline run: give the agent's command after --", file=sys.stderr)
        return 2
    try:
        faults = parse_all(args.fault)
    except FaultError as exc:
        print(f"faultline run: {exc}", file=sys.stderr)
        return 2
    # Names given on the command line come first: they say which variable this agent reads.
    names = list(dict.fromkeys([*args.base_url_env, *BASE_URL_ENVS]))
    upstreams = upstreams_of(args.upstream, names)
    if not any(upstreams.values()):
        print(
            f"faultline run: no upstream. None of {', '.join(names)} is set; "
            "pass --upstream URL, the model API base URL the agent would use.",
            file=sys.stderr,
        )
        return 2
    trace = args.trace or Path(tempfile.mkdtemp(prefix="faultline-run-"))
    trace.mkdir(parents=True, exist_ok=True)
    try:
        from faultline.proxy.model import ModelProxy  # aiohttp loads only for a run
    except ImportError as exc:
        print(
            f"faultline run: the proxy needs the proxy extra ({exc.name} is missing): "
            "pip install 'faultline[proxy]'",
            file=sys.stderr,
        )
        return 2
    recorder = Recorder(
        trace.resolve() / "model.jsonl", secrets.token_hex(16), secrets.token_hex(8)
    )
    model_faults = [fault for fault in faults if isinstance(fault, ModelFault)]
    proxy = ModelProxy(upstreams, model_faults, recorder)
    code = asyncio.run(launch(proxy, recorder, command, args.fault))
    # The verdict reads the run back from its trace, as Find reads any run.
    files = sorted(recorder.path.parent.glob("*.jsonl"))
    runs = [r for r in read_runs_many(files).runs if r.trace_id == recorder.trace_id]
    verdict = judge(
        runs[0] if runs else None,
        recorder.trace_id,
        code,
        args.expect,
        recorder.last_unparsed,
    )
    print(line(verdict), file=sys.stderr)
    return code


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


async def launch(
    proxy: "ModelProxy",
    recorder: Recorder,
    command: Sequence[str],
    faults: Sequence[str],
) -> int:
    """Run the command under the proxy and return the exit code a shell would give."""
    url = await proxy.start()
    env = dict(os.environ)
    routed = {name: up for name, up in proxy.upstreams_by_name().items() if up}
    env.update({name: proxy.url_for(name) for name in routed})
    env.update(
        {
            TRACE_DIR: str(recorder.path.parent),
            TRACE_ID: recorder.trace_id,
            ROOT_SPAN_ID: recorder.root_span_id,
            FAULTS: json.dumps(list(faults)),
        }
    )
    for name, upstream in routed.items():
        print(f"faultline: {name}={proxy.url_for(name)} -> {upstream}", file=sys.stderr)
    for name in proxy.upstreams_by_name().keys() - routed.keys():
        print(
            f"faultline: {name} not set, its calls do not go through", file=sys.stderr
        )
    print(f"faultline: proxy {url}, spans in {recorder.path.parent}", file=sys.stderr)
    started = time.time_ns()
    code = await supervise(command, env)
    await proxy.stop()  # after the agent, so its last calls still go through
    recorder.flush()
    recorder.root(started, time.time_ns(), command, code)
    return code


async def supervise(command: Sequence[str], env: dict[str, str]) -> int:
    """Run the agent, pass signals on to it, and return its exit code as a shell gives it."""
    loop = asyncio.get_running_loop()
    relay = Relay()
    for number in RELAYED:
        loop.add_signal_handler(number, relay.receive, number)
    try:
        try:
            child = await asyncio.create_subprocess_exec(*command, env=env)
        except OSError as exc:
            print(f"faultline run: cannot start {command[0]}: {exc}", file=sys.stderr)
            return 127 if isinstance(exc, FileNotFoundError) else 126
        relay.attach(child)
        code = await child.wait()
    finally:
        for number in RELAYED:
            loop.remove_signal_handler(number)
    return 128 - code if code < 0 else code  # killed by signal N: 128 + N
