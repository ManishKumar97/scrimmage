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
import signal
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

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
    proxy = ModelProxy(upstream)
    return asyncio.run(launch(proxy, command, names, trace, args.fault))


async def launch(
    proxy: "ModelProxy",
    command: Sequence[str],
    names: Sequence[str],
    trace: Path,
    faults: Sequence[str],
) -> int:
    """Run the command under the proxy and return the exit code a shell would give."""
    url = await proxy.start()
    env = dict(os.environ)
    env.update(dict.fromkeys(names, url))
    env.update(
        FAULTLINE_TRACE_DIR=str(trace.resolve()),
        FAULTLINE_TRACE_ID=secrets.token_hex(16),
        FAULTLINE_ROOT_SPAN_ID=secrets.token_hex(8),
        FAULTLINE_FAULTS=json.dumps(list(faults)),
    )
    print(
        f"faultline: proxy {url} -> {proxy.upstream}, spans in {trace}", file=sys.stderr
    )
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
        await proxy.stop()  # after the agent, so its last calls still go through
    return 128 - code if code < 0 else code  # killed by signal N: 128 + N


RELAYED = (signal.SIGINT, signal.SIGTERM)


class Relay:
    """Sends Ctrl-C and SIGTERM on to the agent. Faultline waits for the agent to exit.

    It is in place before the agent starts. A signal that comes while the agent is
    starting is held, then sent once the agent runs.
    """

    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.held: list[signal.Signals] = []

    def receive(self, number: signal.Signals) -> None:
        if number == signal.SIGINT and terminal_sent_it_to_the_child():
            return  # Ctrl-C reached the whole foreground group, the child included
        if self.child is None:
            self.held.append(number)
        elif self.child.returncode is None:
            self.child.send_signal(number)

    def attach(self, child: asyncio.subprocess.Process) -> None:
        self.child = child
        for number in self.held:
            child.send_signal(number)
        self.held.clear()


def terminal_sent_it_to_the_child() -> bool:
    """True when we, and so the child, are the terminal's foreground process group."""
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return False  # no terminal: the signal was sent to faultline alone
