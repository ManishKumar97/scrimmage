"""scrimmage check --service: what scrimmage test would find before its first run.

It reads the token, binds the control server, sends one probe, and prints what the service
reported. It sends no run and no setup request, so it spends nothing and makes no model call.
With [agent] start it starts its own copy of the service first, and stops it after.
"""

import shutil
from dataclasses import dataclass
from typing import TextIO, cast

from scrimmage import runner as running
from scrimmage import service_client as client
from scrimmage import versions
from scrimmage.config import Config
from scrimmage.outcomes import INFRA, MISTAKE, OK

LABEL_WIDTH = 12
OK_LEVEL, WARN, MISTAKE_LEVEL, INFRA_LEVEL = "ok", "warn", "mistake", "infra"


@dataclass
class Line:
    label: str
    text: str
    level: str = OK_LEVEL


@dataclass
class Outcome:
    code: int
    lines: list[Line]
    messages: list[tuple[str, str]]  # (level, text)
    document: dict[str, object]


def control_line(control: dict[str, object], runner: running.Runner) -> Line:
    try:
        client.check_control(
            {"control": control},
            runner.server.runner_id,
            asked_s=runner.asked_s,
            silent=runner.went_silent,
        )
    except client.Setup as setup:
        return Line("control", str(setup), INFRA_LEVEL)
    tls = control.get("tls")
    secure = "TLS verified" if tls == "verified" else "no TLS, on this machine"
    return Line(
        "control", f"{control.get('url')} reached, {secure}, this runner, {rtt(control)} ms"
    )


def rtt(control: dict[str, object]) -> str:
    value = control.get("rtt_ms")
    return f"{value:.0f}" if isinstance(value, int | float) else "?"


def version_line(report: dict[str, object]) -> Line:
    """One line: the same version on both sides, or the mistake naming both."""
    try:
        client.check_version(report)
    except client.Setup as setup:
        return Line("version", str(setup), MISTAKE_LEVEL)
    return Line("version", f"scrimmage {versions.installed()} in the service and here")


def notice_lines(report: dict[str, object]) -> list[Line]:
    notices = cast(list[object], report.get("notices") or [])
    if not notices:
        return [Line("notices", "none")]
    found: list[Line] = []
    for entry in notices:
        fields = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        found.append(
            Line("notices" if not found else "", f"{fields.get('message') or entry}", WARN)
        )
    return found


def inspect(runner: running.Runner, probed: client.Probed) -> Outcome:
    """Every line of the report, each judged on its own, so one problem hides no other."""
    agent = runner.config.agent
    report = probed.report
    assert report is not None  # probe_service raised without one
    lines: list[Line] = []
    messages: list[tuple[str, str]] = []
    gates = ", ".join(str(g) for g in cast(list[object], report.get("gates") or []))
    lines.append(
        Line("service", f"POST {agent.service}   gated ({gates}), pid {report.get('pid')}")
    )
    lines.append(version_line(report))
    python = " ".join(str(report.get("python", "")).split())
    lines.append(Line("python", f"{python} at {report.get('executable')}"))
    tools = sorted(str(t) for t in cast(list[object], report.get("tools") or []))
    problem = runner.tools_problem(report)
    if problem is None:
        lines.append(Line("tools", f"{', '.join(tools) or 'none'}   (match the file)"))
    else:
        lines.append(Line("tools", problem, MISTAKE_LEVEL))
    control = cast(dict[str, object], report.get("control") or {})
    lines.append(control_line(control, runner))
    lines += notice_lines(report)
    for line in lines:
        if line.level in (WARN, MISTAKE_LEVEL, INFRA_LEVEL) and line.label != "notices":
            messages.append((line.level, line.text))
    first = next((line for line in lines if line.level in (MISTAKE_LEVEL, INFRA_LEVEL)), None)
    code = OK if first is None else MISTAKE if first.level == MISTAKE_LEVEL else INFRA
    document: dict[str, object] = {
        "url": str(agent.service),
        "gates": cast(list[object], report.get("gates") or []),
        "pid": report.get("pid"),
        "sdk": report.get("sdk"),
        "runner_sdk": versions.installed(),
        "python": report.get("python"),
        "executable": report.get("executable"),
        "tools": tools,
        "tools_match": problem is None,
        "control": control,
        "notices": cast(list[object], report.get("notices") or []),
    }
    return Outcome(code, lines, messages, document)


def print_lines(lines: list[Line], out: TextIO) -> None:
    for line in lines:
        out.write(f"{line.label:<{LABEL_WIDTH}}{line.text}\n")


def check(config: Config, stop_flag: list[str] | None) -> Outcome | client.Setup:
    """Probe the service of config and read its report. A Setup: the service did not answer
    as a gated one, or the runner's own environment is not ready.

    SIGINT and SIGTERM are caught as scrimmage test catches them: the wait ends with
    Interrupted, and a copy that start began is stopped before the handlers go back, so a
    signal while it stops is only noted."""
    runner: running.Runner | None = None
    release = running.nothing
    try:
        runner = running.Runner(config, stop_flag=stop_flag, scratch=True)
        release = runner.catch_signals()
        runner.server.start()
        if runner.managed is not None:
            runner.managed.start(runner.control_url(runner.server.url))
        probed = runner.probe_service()
        outcome = inspect(runner, probed)
    except client.Setup as setup:
        outcome = setup
    finally:
        if runner is not None:
            try:
                try:
                    if runner.managed is not None:
                        runner.managed.stop()
                finally:
                    release()
                    runner.server.stop()
            finally:
                shutil.rmtree(runner.folder.parent, ignore_errors=True)
                shutil.rmtree(runner.answers, ignore_errors=True)
    if (
        runner is not None and runner.stop_flag
    ):  # it came after the probe, or while the copy stopped
        raise running.Interrupted(runner.stop_flag[0])
    return outcome
