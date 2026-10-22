"""Run incidents one at a time: inject, wait for the page, ask the agent, recover."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from faultline.ida import RunResult
from faultline.incidents import records as rec
from faultline.incidents.catalog import Catalog, Incident
from faultline.incidents.health import Health
from faultline.incidents.records import Record
from faultline.incidents.webhook import (
    Alert,
    AlertInbox,
    build_question,
    synthetic_notification,
)
from faultline.verdict import score

SETUP_TIMEOUT = 20 * 60.0  # seconds to wait for a healthy test bed
ALERT_TIMEOUT = 20 * 60.0  # seconds to wait for a page after injecting
RECOVER_CALL_TIMEOUT = 60.0  # the cleanup call, even when the trial was cancelled
MAX_SETUP_FAILURES = 2  # in a row, then the run stops: the test bed is not coming back

# The control is a false alarm: a symptom page while nothing is wrong. It is the same
# kind of page the other trials get, so the agent cannot tell it apart by its form.
CONTROL_LABELS = {
    "alertname": "FrontendErrorRate",
    "severity": "page",
    "service_name": "frontend",
}
CONTROL_DELAY = 300.0  # seconds
CONTROL_SUMMARY = "frontend fails 2% of its requests"


class SetupAborted(RuntimeError):
    """Too many trials in a row could not start. The results file is safe to resume."""


def keep_nothing(record: Record) -> None:
    pass


@dataclass
class Deps:
    """Everything a trial touches outside itself, so tests can stand in for each."""

    wait_healthy: Callable[[float], Awaitable[Health]]
    inject: Callable[[Incident], Awaitable[None]]
    recover_all: Callable[[], Awaitable[dict[str, str]]]
    diagnose: Callable[[str, Path], Awaitable[RunResult]]
    reset_traces: Callable[[], Awaitable[None]]
    sleep: Callable[[float], Awaitable[None]]
    inbox: AlertInbox
    now: Callable[[], datetime]
    sha: str
    trace_dir: Path
    setup_timeout: float = SETUP_TIMEOUT
    alert_timeout: float = ALERT_TIMEOUT
    # The control has no fault to take time to show. It waits this long, as the other
    # trials wait for their page, so the traces it is asked about have refilled.
    control_delay: float = CONTROL_DELAY


async def page_or_none(
    inbox: AlertInbox, timeout: float
) -> tuple[Alert, list[str]] | None:
    """The next page, or None at the timeout. Stale alerts never reach the queue."""
    try:
        async with asyncio.timeout(timeout):
            return await inbox.queue.get()
    except TimeoutError:
        return None


async def run_trial(
    deps: Deps,
    name: str,
    trial: int,
    incident: Incident | None,
    save: Callable[[Record], None] = keep_nothing,
) -> Record:
    """One trial. `incident` None is the control. Always leaves the flags off.

    `save` is called with the finished record, or earlier if the run is cancelled
    while the test bed recovers, so a trial the agent already answered is never lost.
    """
    started = deps.now()
    expected = incident.service if incident else "none"
    record = Record(
        sha=deps.sha,
        incident=name,
        trial=trial,
        outcome=rec.SETUP_FAILED,
        started_at=started.isoformat(),
        expected=expected,
    )
    try:
        await run_steps(deps, record, incident)
    finally:
        await recover(deps, record, save)
    save(record)
    return record


async def run_steps(deps: Deps, record: Record, incident: Incident | None) -> None:
    # A fault left on by an earlier run must be off before the health check.
    try:
        await deps.recover_all()
    except Exception as exc:  # noqa: BLE001 - reported on the record
        record.detail = f"could not clear flags: {exc}"
        return
    health = await deps.wait_healthy(deps.setup_timeout)
    if not health.ok:
        record.detail = f"test bed not healthy: {health.describe()}"
        return

    # The last trial's failed traces would still be in Jaeger and mislead this one.
    try:
        await deps.reset_traces()
    except Exception as exc:  # noqa: BLE001 - reported on the record
        record.detail = f"could not reset traces: {exc}"
        return

    injected_at = deps.now()
    deps.inbox.reset(since=injected_at)
    try:
        if incident:
            await deps.inject(incident)
        else:
            await deps.sleep(deps.control_delay)
            note = synthetic_notification(CONTROL_LABELS, CONTROL_SUMMARY, injected_at)
            deps.inbox.accept(note)
    except Exception as exc:  # noqa: BLE001 - reported on the record
        record.detail = f"injection failed: {exc}"
        return

    page = await page_or_none(deps.inbox, deps.alert_timeout)
    if page is None:
        record.outcome = rec.NO_ALERT
        record.detail = f"no alert within {deps.alert_timeout:g}s"
        return
    alert, names = page
    record.alert, record.alerts = alert.name, names
    record.question = build_question(alert)

    trace_file = deps.trace_dir / f"{record.incident}-{record.trial}.jsonl"
    record.trace_file = str(trace_file)
    try:
        result = await deps.diagnose(record.question, trace_file)
    except Exception as exc:  # noqa: BLE001 - a crashed run is a result, not a crash of the batch
        record.outcome = rec.AGENT_ERROR
        record.stop_reason = "exception"
        record.detail = f"{type(exc).__name__}: {exc}"
        return
    record.answer = result.answer
    record.stop_reason = result.stop_reason
    record.steps = result.steps
    record.tokens = result.tokens
    record.cache_read_tokens = result.cache_read_tokens
    record.seconds = result.seconds
    verdict = score(result.answer, incident.service if incident else None)
    record.service = verdict.service
    record.outcome = verdict.outcome
    record.correct = verdict.outcome == "correct"
    record.detail = ""


async def recover(deps: Deps, record: Record, save: Callable[[Record], None]) -> None:
    """Turn every flag off and wait for the test bed to come back.

    Runs on every exit path, including a cancelled trial. A test bed that does not
    come back is written on the record, so the next trial is not blamed for it.
    """
    # A cancel must not abort the flags-off call half way: wait for it, it has its own
    # timeout. The cancel is raised again once the flags are off and the record is kept.
    call = asyncio.ensure_future(
        asyncio.wait_for(deps.recover_all(), RECOVER_CALL_TIMEOUT)
    )
    cancelled = False
    while not call.done():
        try:
            await asyncio.wait({call})
        except asyncio.CancelledError:
            cancelled = True
    failure = asyncio.CancelledError() if call.cancelled() else call.exception()
    if failure is not None:
        record.recovery = f"could not clear flags: {failure!r}"
    if cancelled:
        if failure is None:
            record.recovery = "interrupted before the test bed was confirmed healthy"
        save(record)
        raise asyncio.CancelledError
    if failure is not None:
        return
    if record.outcome == rec.SETUP_FAILED:
        record.recovery = "not needed"  # nothing was injected
        return
    try:
        health = await deps.wait_healthy(deps.setup_timeout)
    except BaseException:  # a cancel or a crash must not lose a trial already answered
        record.recovery = "interrupted before the test bed was confirmed healthy"
        save(record)
        raise
    record.recovery = "ok" if health.ok else f"not healthy: {health.describe()}"
    deps.inbox.reset()


def plan(
    catalog: Catalog, trials: int, only: list[str] | None
) -> list[tuple[str, int]]:
    """Trial order: round by round, every incident then the control."""
    names = [i.name for i in catalog.incidents] + [catalog.control.name]
    if only:
        unknown = set(only) - set(names)
        if unknown:
            raise ValueError(f"unknown incident: {sorted(unknown)}")
        names = [n for n in names if n in only]
    return [(name, t) for t in range(1, trials + 1) for name in names]


async def run_all(
    deps: Deps,
    catalog: Catalog,
    out: Path,
    trials: int,
    only: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> list[Record]:
    """Run every planned trial not already in `out`. Append a line per trial."""
    done = rec.finished(rec.load(out))
    ran = []
    setup_failures = 0  # in a row
    for name, trial in plan(catalog, trials, only):
        if (name, trial) in done:
            log(f"skip {name} #{trial}: already in {out}")
            continue
        incident = None if name == catalog.control.name else catalog.get(name)
        log(f"start {name} #{trial}")
        record = await run_trial(deps, name, trial, incident, partial(rec.append, out))
        ran.append(record)
        log(f"done  {name} #{trial}: {record.outcome} (recovery {record.recovery})")
        setup_failures = setup_failures + 1 if record.outcome == rec.SETUP_FAILED else 0
        if setup_failures >= MAX_SETUP_FAILURES:
            raise SetupAborted(
                f"{setup_failures} trials in a row could not start "
                f"(last: {record.detail}). Fix the test bed and rerun with "
                f"--out {out} to resume."
            )
    return ran
