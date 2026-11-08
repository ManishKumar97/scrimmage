"""Switch the SDK on in a program scrimmage-run started with SCRIMMAGE_TOKEN set.

The program the runner started fetches its plan before the team's code runs. If it cannot,
the program ends here, so the agent never runs without its plan. A child process prints
nothing and fetches its plan at its first gated call, so a short command costs nothing.
A child whose plan cannot be fetched refuses each call, and tries again for the next one.
"""

import atexit
import os
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import NoReturn

from scrimmage_core import (
    http_gate,
    labels,
    langchain_gate,
    mcp_gate,
    process_gate,
    state,
    threads,
    tool_gate,
)
from scrimmage_core.client import ControlClient, ControlError, Sender
from scrimmage_core.mocks import allowed
from scrimmage_core.plan import Plan, no_plan, tools_from

TERM_WAIT_S = 4.0  # under the runner's five seconds before SIGKILL


def stop(message: str) -> NoReturn:
    print(
        f"scrimmage: {message}; the program stops before the agent runs",
        file=sys.stderr,
    )
    sys.stderr.flush()
    os._exit(1)


def report_tools(run_state: state.RunState) -> None:
    # A child made only some of the agent's tools, so it does not speak for them.
    if run_state.child:
        return
    # A blocked tool is still one the agent has: its label is spelled right.
    tools = sorted(run_state.seen | run_state.blocked.keys())
    run_state.notice("tools_seen", "scrimmage", "the tools the agent has", tools=tools)


def outside(plan: Plan, prefixes: list[str]) -> str | None:
    """The first import path in the plan that SCRIMMAGE_ALLOW_IMPORTS does not cover."""
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    return next((path for path in paths if not allowed(path, prefixes)), None)


def script_folder() -> list[str]:
    """Python adds a script's folder to its path after this file has run."""
    script = sys.argv[0]
    return [] if script in ("", "-c", "-m") else [os.path.dirname(os.path.abspath(script))]


def runs_as_main(module: str, target: str | None, folders: list[str]) -> bool:
    """Whether this module is the program itself, which runs as __main__ and is never
    imported, so no import can put a gate on its tools. target is the -m module, if any."""
    if target is not None:
        return module in (target, f"{target}.__main__")
    spec = tool_gate.module_spec(module, folders)
    if spec is None or spec.origin is None or sys.argv[0] in ("", "-c"):
        return False
    return os.path.realpath(spec.origin) == os.path.realpath(sys.argv[0])


def handle_sigterm(last_lines: Callable[[], None]) -> None:
    """On SIGTERM send the last record lines, then end as Python would have with no handler.

    The handler runs on the main thread, which may hold a lock the lines need, so a thread
    does the sending and the wait for it has a limit. A program with a handler of its own
    replaces this one. The sender thread has then sent all but the last 100 ms.
    """
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return

    def on_term(signum: int, frame: FrameType | None) -> None:
        del frame
        signal.signal(signum, signal.SIG_IGN)  # the runner may send it twice
        try:
            worker = threading.Thread(target=last_lines, daemon=True)
            threads.start(worker)
            worker.join(TERM_WAIT_S)
        except RuntimeError:  # Python is already shutting down, so no new thread
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, on_term)


def start(*, child: bool = False, target: str | None = None) -> None:
    token = os.environ["SCRIMMAGE_TOKEN"]
    url = os.environ.get("SCRIMMAGE_CONTROL_URL", "")
    run = os.environ.get("SCRIMMAGE_RUN", "")
    if not url or not run:
        stop("SCRIMMAGE_CONTROL_URL and SCRIMMAGE_RUN must be set")
    prefixes = [p for p in os.environ.get("SCRIMMAGE_ALLOW_IMPORTS", "").split(",") if p]
    plan: Plan | None = None
    try:
        client = ControlClient(url, token)
        if not child:
            plan = Plan.from_json(client.fetch_plan(run))
    except (ControlError, ValueError) as exc:
        stop(f"cannot get the plan for {run} from {url}: {exc}")
    if plan is not None and (path := outside(plan, prefixes)) is not None:
        stop(f"{path} is outside SCRIMMAGE_ALLOW_IMPORTS ({','.join(prefixes) or 'empty'})")

    def load() -> Plan:
        """A child's plan, at its first gated call. With no plan, the call is refused."""
        try:
            fetched = Plan.from_json(client.fetch_plan(run))
        except (ControlError, ValueError) as exc:
            return no_plan(run, str(exc))
        path = outside(fetched, prefixes)
        return fetched if path is None else no_plan(run, f"{path} is outside the allowed imports")

    # G2: a child's code imports its tools before any plan could arrive.
    tools, renames = tools_from(os.environ.get("SCRIMMAGE_TOOLS", ""))
    names = tuple(labels.tool_name(path, dict(renames)) for path in tools)
    sender = Sender(client, run, eager=child)
    run_state = state.RunState(plan, sender, prefixes, load=load, tools=names, blobs=sender)
    sender.proc = run_state.proc
    sender.on_closed = run_state.run_closed
    if plan is not None:
        tools, renames = plan.tools, plan.renames
        sender.start()
        run_state.announce()
    atexit.register(sender.flush)
    atexit.register(run_state.stop_open)  # a call on a daemon thread, after the replies below
    if not tools and not child:  # registered after the flush, so it runs first
        # With no tools list the runner cannot know the names; a framework's adapter does.
        atexit.register(lambda: report_tools(run_state))
    state.install(run_state)
    folders = [os.getcwd(), *script_folder()]
    for module, tool_names in tool_gate.targets(tools).items():
        message = None
        if runs_as_main(module, target, folders):
            message = (
                f"{module} is the program itself, so {tool_names[0]} cannot be gated. Move the "
                "tool into a module the program imports"
            )
        elif not child and tool_gate.module_spec(module, folders) is None:
            message = f"{module} is listed under [agent] tools but cannot be found"
        if message is not None:
            run_state.cannot_gate(tool_names[0], message)
            sender.flush()
            stop(message)
    tool_gate.install(tools, dict(renames))

    def cannot_gate(message: str) -> None:
        sender.flush()
        stop(message)

    ctx = http_gate.install(run_state, client, sender, cannot_gate)
    langchain_gate.install(run_state, cannot_gate)
    mcp_gate.install(run_state, sender, cannot_gate)

    def forked() -> None:
        run_state.forked()
        sender.forked(run_state.proc)
        ctx.open.clear()  # replies the parent is reading

    os.register_at_fork(after_in_child=forked)

    def last_lines() -> None:
        ctx.close_open()
        run_state.stop_open()
        sender.flush()

    def end_program() -> None:
        """scrimmage init --probe has what it came for, so no more of the team's code runs."""
        last_lines()
        os._exit(0)

    ctx.end_program = end_program

    def before_exec() -> None:
        """exec ends this image with no exit steps, and if it fails the program goes on."""
        last_lines()
        sender.eager = True

    process_gate.install(run_state, before_exec)
    handle_sigterm(last_lines)
    if plan is not None:
        print(
            f"scrimmage: active for run {run} ({plan.test}, {plan.row}); "
            "calls follow this test's plan",
            file=sys.stderr,
        )
