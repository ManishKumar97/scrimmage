"""Switch the SDK on in a program faultline-run started with FAULTLINE_TOKEN set.

The plan is fetched before the team's code runs. If it cannot be fetched, the program
ends here, so the agent never runs without its plan.
"""

import atexit
import os
import sys
from typing import NoReturn

from faultline_sdk import state, tool_gate
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
    prefixes = [p for p in os.environ.get("FAULTLINE_ALLOW_IMPORTS", "").split(",") if p]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type)]
    for path in paths:
        if not allowed(path, prefixes):
            stop(f"{path} is outside FAULTLINE_ALLOW_IMPORTS ({','.join(prefixes) or 'empty'})")
    sender = Sender(client, run)
    sender.start()
    atexit.register(sender.flush)
    state.install(state.RunState(plan, sender, prefixes))
    tool_gate.install(plan.tools)
    print(
        f"faultline: active for run {run} ({plan.row}); calls follow this test's plan",
        file=sys.stderr,
    )
