"""Switch the SDK on in a program faultline-run started with FAULTLINE_TOKEN set.

The plan is fetched before the team's code runs. If it cannot be fetched, the program
ends here, so the agent never runs without its plan.
"""

import atexit
import dataclasses
import os
import sys
from typing import NoReturn

from faultline_sdk import httpx2_gate, langchain_gate, state, tool_gate
from faultline_sdk.client import ControlClient, ControlError, Sender
from faultline_sdk.mocks import allowed
from faultline_sdk.plan import Plan


def stop(message: str) -> NoReturn:
    print(
        f"faultline: {message}; the program stops before the agent runs",
        file=sys.stderr,
    )
    sys.stderr.flush()
    os._exit(1)


def report_tools(run_state: state.RunState) -> None:
    run_state.notice(
        "tools_seen", "faultline", "the tools a gate reaches", tools=sorted(run_state.seen)
    )


def start() -> None:
    token = os.environ["FAULTLINE_TOKEN"]
    url = os.environ.get("FAULTLINE_CONTROL_URL", "")
    run = os.environ.get("FAULTLINE_RUN", "")
    if not url or not run:
        stop("FAULTLINE_CONTROL_URL and FAULTLINE_RUN must be set")
    try:
        client = ControlClient(url, token)
        plan = Plan.from_json(client.fetch_plan(run))
    except (ControlError, ValueError) as exc:
        stop(f"cannot get the plan for {run} from {url}: {exc}")
    root = os.environ.get("FAULTLINE_ROOT_PID", "")
    child = root.isdigit() and int(root) != os.getpid()
    if child:
        # Each process counts its own calls, so faults fire only in the program the
        # runner started. A child keeps the mocks and the safety rules.
        plan = dataclasses.replace(plan, faults=())
    prefixes = [p for p in os.environ.get("FAULTLINE_ALLOW_IMPORTS", "").split(",") if p]
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    for path in paths:
        if not allowed(path, prefixes):
            stop(f"{path} is outside FAULTLINE_ALLOW_IMPORTS ({','.join(prefixes) or 'empty'})")
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

    httpx2_gate.install(run_state, client, sender, cannot_gate)
    langchain_gate.install(run_state, cannot_gate)
    where = " in a child process, without faults" if child else ""
    print(
        f"faultline: active for run {run} ({plan.test}, {plan.row}){where}; "
        "calls follow this test's plan",
        file=sys.stderr,
    )
