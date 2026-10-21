"""Run incidents one at a time: inject, wait for the page, ask the agent, recover."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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

# The control is a false alarm: a symptom page while nothing is wrong. It is the same
# kind of page the other trials get, so the agent cannot tell it apart by its form.
CONTROL_LABELS = {
    "alertname": "FrontendErrorRate",
    "severity": "page",
    "service_name": "frontend",
}
CONTROL_SUMMARY = "frontend fails 2% of its requests"


@dataclass
class Deps:
    """Everything a trial touches outside itself, so tests can stand in for each."""

    wait_healthy: Callable[[float], Awaitable[Health]]
    inject: Callable[[Incident], Awaitable[None]]
    recover_all: Callable[[], Awaitable[dict[str, str]]]
    diagnose: Callable[[str, Path], Awaitable[RunResult]]
    inbox: AlertInbox
    now: Callable[[], datetime]
    sha: str
    trace_dir: Path
    setup_timeout: float = SETUP_TIMEOUT
    alert_timeout: float = ALERT_TIMEOUT


async def wait_for_page(
    inbox: AlertInbox, since: datetime, timeout: float
) -> tuple[Alert, list[str]] | None:
    """The first page for an alert that started after `since`, or None at the timeout.

    A late notification for the previous trial's alert starts before `since`, so it
    cannot pass for this trial's page.
    """
    async with asyncio.timeout(timeout):
        while True:
            page, names = await inbox.queue.get()
            if page.starts_at is None or page.starts_at >= since:
                return page, names


async def page_or_none(
    inbox: AlertInbox, since: datetime, timeout: float
) -> tuple[Alert, list[str]] | None:
    try:
        return await wait_for_page(inbox, since, timeout)
    except TimeoutError:
        return None


async def run_trial(
    deps: Deps, name: str, trial: int, incident: Incident | None
) -> Record:
    """One trial. `incident` None is the control. Always leaves the flags off."""
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
        await recover(deps, record)
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

    deps.inbox.reset()
    injected_at = deps.now()
    try:
        if incident:
            await deps.inject(incident)
        else:
            note = synthetic_notification(CONTROL_LABELS, CONTROL_SUMMARY, injected_at)
            deps.inbox.accept(note)
    except Exception as exc:  # noqa: BLE001 - reported on the record
        record.detail = f"injection failed: {exc}"
        return

    page = await page_or_none(deps.inbox, injected_at, deps.alert_timeout)
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


async def recover(deps: Deps, record: Record) -> None:
    """Turn every flag off and wait for the test bed to come back.

    Runs on every exit path, including a cancelled trial. A test bed that does not
    come back is written on the record, so the next trial is not blamed for it.
    """
    try:
        async with asyncio.timeout(RECOVER_CALL_TIMEOUT):
            await deps.recover_all()
    except Exception as exc:  # noqa: BLE001 - TimeoutError is included
        record.recovery = f"could not clear flags: {exc!r}"
        return
    if record.outcome == rec.SETUP_FAILED:
        record.recovery = "not needed"  # nothing was injected
        return
    health = await deps.wait_healthy(deps.setup_timeout)
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
    for name, trial in plan(catalog, trials, only):
        if (name, trial) in done:
            log(f"skip {name} #{trial}: already in {out}")
            continue
        incident = None if name == catalog.control.name else catalog.get(name)
        log(f"start {name} #{trial}")
        record = await run_trial(deps, name, trial, incident)
        rec.append(out, record)
        ran.append(record)
        log(f"done  {name} #{trial}: {record.outcome} (recovery {record.recovery})")
    return ran


def utc_now() -> datetime:
    return datetime.now(UTC)
