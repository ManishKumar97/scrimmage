"""Test support, never the product's: changes one mock of a claimed run's plan in this process.

With SCRIMMAGE_TEST_CHANGE_PLAN set, the gate's claim is wrapped: the claim and its reply are
untouched, and the plan the run's state holds then differs from the one the runner sent. A
plan digest taken over the run's state must differ from the runner's, and one taken over the
reply must not. It lives in the test app so any image built from the test fixtures has it.
"""

import dataclasses
import os
from collections.abc import Awaitable, Callable
from typing import Any

ENV = "SCRIMMAGE_TEST_CHANGE_PLAN"


def install() -> None:
    if not os.environ.get(ENV):
        return
    # Imported only here: a process that does not switch the seam on, such as a team's spawned
    # worker, must not load Scrimmage because the test app does.
    from scrimmage import asgi_gate

    if getattr(asgi_gate.claimed, "changes_the_plan", False):
        return
    claimed: Callable[..., Awaitable[Any]] = asgi_gate.claimed

    async def claimed_then_changed(gate: Any, name: str) -> Any:
        run, sender = await claimed(gate, name)
        plan = run.plan
        if not plan.mocks:
            raise RuntimeError(f"{ENV} needs a plan with a mock to change, and {name} has none")
        first = dataclasses.replace(plan.mocks[0], returns="changed by the test")
        first = dataclasses.replace(first, status=first.status + 1)
        run._plan = dataclasses.replace(plan, mocks=(first, *plan.mocks[1:]))  # pyright: ignore[reportPrivateUsage]
        return run, sender

    claimed_then_changed.changes_the_plan = True  # type: ignore[attr-defined]
    asgi_gate.claimed = claimed_then_changed  # type: ignore[assignment]
