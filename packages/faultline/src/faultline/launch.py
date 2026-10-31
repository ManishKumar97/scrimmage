"""`faultline run`: start the proxy, run the agent through it, exit when the agent does.

    faultline run [--fault SPEC]... [--upstream URL] [--base-url-env NAME]...
                  [--trace DIR] -- CMD...

Model SDKs read their base URL from an environment variable. The agent gets the
proxy's URL in those variables, so its model calls go through the proxy with no
change to its code. The proxy sends them on to the upstream, the URL the agent
would have used.
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

from faultline.proxy.record import ROOT_SPAN_ID, TRACE_DIR, TRACE_ID, Recorder
from faultline.relay import RELAYED, Relay

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
        help="a fault to inject, such as tool:get_logs:empty (repeatable)",
    )
    parser.add_argument(
        "--upstream",
        metavar="URL",
        help="the model API base URL to forward to "
        "(default: the value of the first base URL variable that is set)",
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
    # Names given on the command line come first: they say which variable this agent reads.
    names = list(dict.fromkeys([*args.base_url_env, *BASE_URL_ENVS]))
    upstream = args.upstream or next(
        (os.environ[name] for name in names if os.environ.get(name)), None
    )
    if not upstream:
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
    proxy = ModelProxy(upstream, recorder)
    return asyncio.run(launch(proxy, recorder, command, names, args.fault))


async def launch(
    proxy: "ModelProxy",
    recorder: Recorder,
    command: Sequence[str],
    names: Sequence[str],
    faults: Sequence[str],
) -> int:
    """Run the command under the proxy and return the exit code a shell would give."""
    url = await proxy.start()
    env = dict(os.environ)
    env.update(dict.fromkeys(names, url))
    env.update(
        {
            TRACE_DIR: str(recorder.path.parent),
            TRACE_ID: recorder.trace_id,
            ROOT_SPAN_ID: recorder.root_span_id,
            "FAULTLINE_FAULTS": json.dumps(list(faults)),
        }
    )
    print(
        f"faultline: proxy {url} -> {proxy.upstream}, spans in {recorder.path.parent}",
        file=sys.stderr,
    )
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
