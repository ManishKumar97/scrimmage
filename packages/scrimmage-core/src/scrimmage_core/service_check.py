"""scrimmage check --service: what scrimmage test would find before its first run.

It reads the token, binds the control server, sends one probe and one GET for /openapi.json,
and prints what the service reported. It sends no run and no setup request, so it spends
nothing and makes no model call.
"""

import shutil
from dataclasses import dataclass
from typing import TextIO, cast

from scrimmage_core import runner as running
from scrimmage_core import service_client as client
from scrimmage_core import versions
from scrimmage_core.config import Config
from scrimmage_core.outcomes import INFRA, MISTAKE, OK

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


def control_line(control: dict[str, object], runner_id: str) -> Line:
    try:
        client.check_control({"control": control}, runner_id, asked_s=running.READY_S)
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


def versions_line(report: dict[str, object]) -> tuple[Line, list[str]]:
    """The version line, and the warning that goes with it, if any."""
    ours = versions.installed()
    theirs = versions.shown(report.get("sdk"))
    try:
        warnings = client.check_versions(report)
    except client.Setup as setup:
        return Line("versions", str(setup), MISTAKE_LEVEL), []
    text = f"scrimmage-core {theirs} in the service, {ours} here"
    return Line("versions", text, WARN if warnings else OK_LEVEL), warnings


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


def inspect(runner: running.Runner, probed: client.Probed, strict: bool) -> Outcome:
    """Every line of the report, each judged on its own, so one problem hides no other."""
    agent = runner.config.agent
    report = probed.report
    assert report is not None  # probe_service raised without one
    _, _, path = client.target(agent)
    lines: list[Line] = []
    messages: list[tuple[str, str]] = []
    gates = ", ".join(str(g) for g in cast(list[object], report.get("gates") or []))
    lines.append(
        Line("service", f"POST {agent.service}   gated ({gates}), pid {report.get('pid')}")
    )
    vline, vwarnings = versions_line(report)
    lines.append(vline)
    messages += [(WARN, text) for text in vwarnings]
    python = " ".join(str(report.get("python", "")).split())
    lines.append(Line("python", f"{python} at {report.get('executable')}"))
    tools = sorted(str(t) for t in cast(list[object], report.get("tools") or []))
    problem = runner.tools_problem(report)
    if problem is None:
        lines.append(Line("tools", f"{', '.join(tools) or 'none'}   (match the file)"))
    else:
        lines.append(Line("tools", problem, MISTAKE_LEVEL))
    control = cast(dict[str, object], report.get("control") or {})
    lines.append(control_line(control, runner.server.runner_id))
    gap = probed.gap
    gap_line = Line("clock", "the service sent no time", WARN)
    if gap is not None:
        stopped = abs(gap) >= client.CLOCK_STOP_S
        said = client.clock_stop(gap) if stopped else client.clock_text(gap)
        warned = abs(gap) >= client.CLOCK_WARN_S
        gap_line = Line("clock", said, INFRA_LEVEL if stopped else WARN if warned else OK_LEVEL)
    lines.append(gap_line)
    found = client.template_check(agent)
    if found.warnings:
        lines.append(Line("template", found.warnings[0], MISTAKE_LEVEL if strict else WARN))
        lines += [Line("", w, MISTAKE_LEVEL if strict else WARN) for w in found.warnings[1:]]
    elif found.notes:
        lines.append(Line("template", found.notes[0]))
    else:
        lines.append(Line("template", f"fits POST {path.partition('?')[0]} in /openapi.json"))
    lines += [Line("", note) for note in (found.notes if found.warnings else found.notes[1:])]
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
        "clock_gap_s": None if gap is None else round(gap, 3),
        "template": {"warnings": list(found.warnings), "notes": list(found.notes)},
        "notices": cast(list[object], report.get("notices") or []),
    }
    return Outcome(code, lines, messages, document)


def print_lines(lines: list[Line], out: TextIO) -> None:
    for line in lines:
        out.write(f"{line.label:<{LABEL_WIDTH}}{line.text}\n")


def check(config: Config, strict: bool, stop_flag: list[str] | None) -> Outcome | client.Setup:
    """Probe the service of config and read its report. A Setup: the service did not answer
    as a gated one, or the runner's own environment is not ready."""
    runner: running.Runner | None = None
    try:
        runner = running.Runner(config, stop_flag=stop_flag, scratch=True)
        runner.server.start()
        probed = runner.probe_service()
        return inspect(runner, probed, strict)
    except client.Setup as setup:
        return setup
    finally:
        if runner is not None:
            try:
                runner.server.stop()
            finally:
                shutil.rmtree(runner.folder.parent, ignore_errors=True)
                shutil.rmtree(runner.answers, ignore_errors=True)
