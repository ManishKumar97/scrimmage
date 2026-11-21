"""The wait of a slow fault, and what may end it early.

The delay comes first, then the call runs as the baseline would run it. Whatever ends a wait,
the gate checks three things before the call runs: the run's stop event, the run having
ended while this process lives on, and the time the run has left. In each case the call
never runs and its line says why. A wait never holds a lock, so two slowed calls overlap.
"""

import sys
import time
from collections.abc import Awaitable, Callable

from scrimmage.errors import ScrimmageStopped, ScrimmageTimeLimit
from scrimmage.state import CLOSED, RunState, Step

STEP_S = 0.1  # an async wait ends within this long of a stop, or of a cancel
STOPPED = "run stopped"
TIME_LIMIT = "time limit"
CANCELLED = "cancelled"
CLIENT_TIMEOUT = "client timeout"


def asyncio_sleep() -> Callable[[float], Awaitable[None]] | None:
    """asyncio's sleep, if this code runs in an asyncio task. A program on an asyncio loop has
    imported asyncio; without it, or outside a task, as under trio, there is nothing to wait on."""
    asyncio = sys.modules.get("asyncio")
    try:
        if asyncio is not None and asyncio.current_task() is not None:
            return asyncio.sleep
    except RuntimeError:  # no asyncio loop is running in this thread
        pass
    return None


def left(state: RunState) -> float:
    """The time the run has left, on the clock the run's limit counts."""
    return state.plan.time_limit_s - (time.monotonic() - state.began)


def plan_wait(state: RunState, seconds: float) -> tuple[float, bool]:
    """How long to wait: the delay, or the time the run has left if that is less, and whether
    the limit is what ends the wait."""
    remaining = left(state)
    return max(min(seconds, remaining), 0.0), remaining < seconds


def check(state: RunState, limit_ends: bool) -> None:
    """After a wait, whatever ended it. Raises if the call must not run."""
    if state.stopped.is_set() or state.refused == CLOSED:
        raise ScrimmageStopped("the run was stopped while this call was slowed")
    if limit_ends:
        limit = state.plan.time_limit_s
        raise ScrimmageTimeLimit(f"the run passed its time limit of {limit:g}s while slowed")


def cut_by(error: BaseException) -> str:
    """What ended a wait early, for the line."""
    if isinstance(error, ScrimmageStopped):
        return STOPPED
    if isinstance(error, ScrimmageTimeLimit):
        return TIME_LIMIT
    return CANCELLED


def begun(step: Step) -> float:
    step.waiting_since = time.monotonic()
    return step.waiting_since


def ended(step: Step, since: float, error: BaseException | None = None) -> None:
    """Note how long the call waited, and what ended it if it was cut short."""
    step.waiting_since = None
    step.slowed_ms = int((time.monotonic() - since) * 1000)
    if error is not None:
        step.cut_by = step.cut_by or cut_by(error)


def plan_client(
    state: RunState, seconds: float, read_timeout: float | None
) -> tuple[float, bool, bool]:
    """(how long to wait, whether the run's limit ends it, whether the caller's own timeout
    does). A timeout shorter than the delay ends the wait there, as a server too slow would."""
    pause, limit_ends = plan_wait(state, seconds)
    if read_timeout is not None and read_timeout < pause:
        return read_timeout, False, True
    return pause, limit_ends, False


def wait_sync(
    state: RunState,
    step: Step,
    seconds: float,
    *,
    read_timeout: float | None = None,
    timed_out: Callable[[], BaseException] | None = None,
) -> None:
    """Wait in the calling thread, on the run's stop event. It blocks that thread only, which
    is what a slow call does. A handler that raises in the wait, such as a SIGALRM timeout,
    leaves with the line written by the gate. read_timeout and timed_out are as for
    wait_steps."""
    pause, limit_ends, client_ends = plan_client(state, seconds, read_timeout)
    since = begun(step)
    try:
        state.stopped.wait(pause)
        check(state, limit_ends)
        if client_ends and timed_out is not None:
            step.cut_by = CLIENT_TIMEOUT
            raise timed_out()
    except BaseException as exc:
        ended(step, since, exc)
        raise
    ended(step, since)


async def wait_steps(
    state: RunState,
    step: Step,
    seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    *,
    read_timeout: float | None = None,
    timed_out: Callable[[], BaseException] | None = None,
) -> None:
    """Wait on the event loop in steps of at most STEP_S, looking at the stop event and the
    time left between steps, so a cancel by the caller's own deadline lands within one step.

    read_timeout: the caller's own timeout, if shorter than the delay, the wait ends there with
    the error timed_out builds, as the library would raise for a server that is too slow.
    """
    pause, limit_ends, client_ends = plan_client(state, seconds, read_timeout)
    since = begun(step)
    end = since + pause
    try:
        while not state.stopped.is_set() and (remaining := end - time.monotonic()) > 0:
            await sleep(min(STEP_S, remaining))
        check(state, limit_ends)
        if client_ends and timed_out is not None:
            step.cut_by = CLIENT_TIMEOUT
            raise timed_out()
    except BaseException as exc:
        ended(step, since, exc)
        raise
    ended(step, since)
