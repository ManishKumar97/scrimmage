"""Switch the SDK on in a program derail-run started with DERAIL_TOKEN set.

The plan is fetched before the team's code runs. If it cannot be fetched, the program
ends here, so the agent never runs without its plan.
"""

import atexit
import dataclasses
import os
import signal
import sys
import threading
from types import FrameType
from typing import NoReturn

from derail_sdk import http_gate, langchain_gate, mcp_gate, state, tool_gate
from derail_sdk.client import ControlClient, ControlError, Sender
from derail_sdk.mocks import allowed
from derail_sdk.plan import Plan, no_plan, tools_from

# Under the runner's five seconds before SIGKILL. A child sends as it goes, so it has
# little to wait for, and its main thread may be the one holding the send.
TERM_WAIT_S = 4.0
CHILD_TERM_WAIT_S = 0.5


def stop(message: str) -> NoReturn:
    print(
        f"derail: {message}; the program stops before the agent runs",
        file=sys.stderr,
    )
    sys.stderr.flush()
    os._exit(1)


def report_tools(run_state: state.RunState) -> None:
    # A blocked tool is still one the agent has: its label is spelled right.
    tools = sorted(run_state.seen | run_state.blocked.keys())
    run_state.notice("tools_seen", "derail", "the tools the agent has", tools=tools)


def outside(plan: Plan, prefixes: list[str]) -> str | None:
    """The first import path in the plan that DERAIL_ALLOW_IMPORTS does not cover."""
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    return next((path for path in paths if not allowed(path, prefixes)), None)


def script_folder() -> list[str]:
    """Python adds a script's folder to its path after this file has run."""
    script = sys.argv[0]
    return [] if script in ("", "-c", "-m") else [os.path.dirname(os.path.abspath(script))]


def handle_sigterm(run_state: state.RunState, sender: Sender, ctx: http_gate.Context) -> None:
    """On SIGTERM send the last record lines, then end as Python would have with no handler.

    The handler runs on the main thread, which may hold a lock the lines need, so a thread
    does the sending and the wait for it has a limit. A program with a handler of its own
    replaces this one. The sender thread has then sent all but the last 100 ms.
    """
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return

    def last_lines() -> None:
        ctx.close_open()
        run_state.stop_open()
        sender.flush()

    def on_term(signum: int, frame: FrameType | None) -> None:
        del frame
        signal.signal(signum, signal.SIG_IGN)  # the runner may send it twice
        try:
            worker = threading.Thread(target=last_lines, daemon=True)
            worker.start()
            worker.join(CHILD_TERM_WAIT_S if sender.eager else TERM_WAIT_S)
        except RuntimeError:  # Python is already shutting down, so no new thread
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, on_term)


def start(*, child: bool = False) -> None:
    token = os.environ["DERAIL_TOKEN"]
    url = os.environ.get("DERAIL_CONTROL_URL", "")
    run = os.environ.get("DERAIL_RUN", "")
    if not url or not run:
        stop("DERAIL_CONTROL_URL and DERAIL_RUN must be set")
    try:
        client = ControlClient(url, token)
        plan = Plan.from_json(client.fetch_plan(run))
    except (ControlError, ValueError) as exc:
        stop(f"cannot get the plan for {run} from {url}: {exc}")
    root = os.environ.get("DERAIL_ROOT_PID", "")
    child = root.isdigit() and int(root) != os.getpid()
    if child:
        # Each process counts its own calls, so faults fire only in the program the
        # runner started. A child keeps the mocks and the safety rules.
        plan = dataclasses.replace(plan, faults=())
    prefixes = [p for p in os.environ.get("DERAIL_ALLOW_IMPORTS", "").split(",") if p]
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    for path in paths:
        if not allowed(path, prefixes):
            stop(f"{path} is outside DERAIL_ALLOW_IMPORTS ({','.join(prefixes) or 'empty'})")
    sender = Sender(client, run)
    run_state = state.RunState(plan, sender, prefixes)
    sender.proc = run_state.proc
    sender.start()
    atexit.register(sender.flush)
    if not plan.tools and not child:  # registered after the flush, so it runs first
        # With no tools list the runner cannot know the names; a framework's adapter does.
        # A child made only some of the agent's tools, so it does not speak for them.
        atexit.register(lambda: report_tools(run_state))
    state.install(run_state)
    # A forked child is a child too: it keeps the mocks and the safety rules, no faults.
    os.register_at_fork(after_in_child=run_state.drop_faults)
    for module, names in tool_gate.targets(plan.tools).items():
        if not tool_gate.module_exists(module, [os.getcwd()]):
            message = f"{module} is listed under [agent] tools but cannot be found"
            run_state.cannot_gate(names[0], message)
            sender.flush()
            stop(message)
    tool_gate.install(plan.tools, dict(plan.renames))

    def cannot_gate(message: str) -> None:
        sender.flush()
        stop(message)

    http_gate.install(run_state, client, sender, cannot_gate)
    langchain_gate.install(run_state, cannot_gate)
    mcp_gate.install(run_state, sender, cannot_gate)

    def forked() -> None:
        run_state.forked()
        sender.forked(run_state.proc)
        ctx.open.clear()  # replies the parent is reading

    os.register_at_fork(after_in_child=forked)
    handle_sigterm(run_state, sender, ctx)
    if plan is not None:
        print(
            f"derail: active for run {run} ({plan.test}, {plan.row}); "
            "calls follow this test's plan",
            file=sys.stderr,
        )
