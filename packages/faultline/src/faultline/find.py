"""`faultline find`: read traces, find the faults in them and check how the agent
reacted. `find()` is the engine; the command prints the Report it returns."""

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from faultline.checks import CHECKS, Check
from faultline.config import Config, load_config
from faultline.detect import detect
from faultline.model import Certainty, Fault, FaultKind, Finding, Run
from faultline.runs import show_run
from faultline.sources import JaegerSource, parse_since, parse_source


@dataclass(frozen=True, slots=True)
class RunResult:
    """One run read, with its faults and every check's findings on it."""

    run: Run
    faults: tuple[Fault, ...]
    findings: tuple[Finding, ...]  # flagged or not: a finding says what was judged


@dataclass(frozen=True, slots=True)
class Report:
    runs: tuple[RunResult, ...]  # one per run read, in the order read

    @property
    def faults(self) -> tuple[Fault, ...]:
        return tuple(fault for result in self.runs for fault in result.faults)

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(finding for result in self.runs for finding in result.findings)

    def summary(self) -> dict:
        """Counts for a dashboard. Every fault kind and every check has a key."""
        return {
            "runs": len(self.runs),
            "runs_with_faults": sum(1 for result in self.runs if result.faults),
            "faults": {
                kind.value: sum(f.kind == kind for f in self.faults)
                for kind in FaultKind
            },
            "flagged": {
                check.name: sum(
                    f.check == check.name and f.flagged for f in self.findings
                )
                for check in CHECKS
            },
        }

    def to_json(self) -> dict:
        """The report as plain JSON values: lists of dicts, enum values as strings.
        `version` changes when the shape does."""
        return {
            "version": 1,
            "summary": self.summary(),
            "runs": [
                {
                    "trace_id": result.run.trace_id,
                    "faults": [fault_json(fault) for fault in result.faults],
                    "findings": [
                        finding_json(f, result.run.trace_id) for f in result.findings
                    ],
                }
                for result in self.runs
            ],
        }


def fault_json(fault: Fault) -> dict:
    return {
        "kind": fault.kind.value,
        "trace_id": fault.trace_id,
        "step": fault.step,
        "call_id": fault.call_id,
        "tool": fault.tool,
        "detail": fault.detail,
        "origin": fault.origin.value,
    }


SCOPES = {check.name: check.scope for check in CHECKS}


def finding_json(finding: Finding, trace_id: str) -> dict:
    return {
        "trace_id": trace_id,
        "check": finding.check,
        "scope": SCOPES[finding.check],
        "certainty": finding.certainty.value,
        "flagged": finding.flagged,
        "evidence": finding.evidence,
        "fault": None if finding.fault is None else fault_json(finding.fault),
    }


def find(runs: Iterable[Run], config: Config) -> Report:
    """Detect every run's faults, then run every check on it."""
    results = []
    for run in runs:
        faults = detect(run, config.detect)
        findings = tuple(
            finding
            for check in CHECKS
            for finding in check.judge(run, faults, config.checks)
        )
        results.append(RunResult(run, faults, findings))
    return Report(tuple(results))


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


WIDTH = max(len(check.name) for check in CHECKS)


def certainty_note(check: Check, flagged: int) -> str:
    """A heuristic never says wrong. It says likely, and asks a person to look."""
    if check.certainty is Certainty.HEURISTIC and flagged:
        return "heuristic: likely, review these runs"
    return check.certainty.value


def show_kind(kind: FaultKind, report: Report) -> list[str]:
    """One fault kind: how often it occurred and how the agent reacted."""
    hit = [r for r in report.runs if any(f.kind == kind for f in r.faults)]
    count = sum(f.kind == kind for r in hit for f in r.faults)
    lines = [f"{kind}  {plural(count, 'fault')} in {plural(len(hit), 'run')}"]
    flagged_by_run: dict[str, list[str]] = {}
    for check in CHECKS:
        if check.scope == "fault":  # one finding per fault, so per fault of this kind
            judged = [
                f
                for r in hit
                for f in r.findings
                if f.check == check.name and f.fault and f.fault.kind == kind
            ]
            unit, total = "", count
        elif check.scope == "faulted_run":  # one finding per run that had a fault
            judged = [f for r in hit for f in r.findings if f.check == check.name]
            unit, total = " runs", len(hit)
        else:
            continue
        flagged = [f for f in judged if f.flagged]
        for finding in flagged:
            assert finding.fault is not None  # these checks need a fault
            flagged_by_run.setdefault(finding.fault.trace_id, []).append(check.name)
        counted = f"flagged {len(flagged)} of {len(judged)}{unit}"
        note = certainty_note(check, len(flagged))
        line = f"  {check.name:<{WIDTH}}  {counted:<22}  {note}"
        # A fault the check could not place, or a run with no answer to read.
        if total > len(judged):
            line += f"; not judged: {total - len(judged)}"
        lines.append(line)
    for trace_id, checks in flagged_by_run.items():
        lines.append(f"    {trace_id}  {', '.join(dict.fromkeys(checks))}")
    return lines


def show_across(report: Report) -> list[str]:
    """Checks that judge any run, fault or not."""
    lines = ["Across all runs"]
    for check in CHECKS:
        if check.scope != "run":
            continue
        flagged = [
            (r.run.trace_id, f)
            for r in report.runs
            for f in r.findings
            if f.check == check.name and f.flagged
        ]
        runs = len({trace_id for trace_id, _ in flagged})
        counted = f"flagged {plural(runs, 'run')}"
        note = certainty_note(check, runs)
        lines.append(f"  {check.name:<{WIDTH}}  {counted:<22}  {note}")
        lines += [f"    {trace_id}  {f.evidence}" for trace_id, f in flagged]
    return lines


def show_report(report: Report) -> str:
    summary = report.summary()
    counts = [
        plural(summary["runs"], "run"),
        f"{summary['runs_with_faults']} with faults",
        plural(len(report.faults), "fault"),
        f"{sum(summary['flagged'].values())} flagged",
    ]
    lines = ["Faultline find: " + ", ".join(counts)]
    kinds = [k for k in FaultKind if any(f.kind == k for f in report.faults)]
    for kind in kinds:
        lines += ["", *show_kind(kind, report)]
    if not kinds:
        lines += ["", "No faults found."]
    lines += ["", *show_across(report)]
    return "\n".join(lines)


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "find", help="read traces, find their faults and check the reactions"
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="SPEC",
        help="where traces come from, for example file:traces/ or "
        "jaeger:http://localhost:16686 (repeatable)",
    )
    parser.add_argument(
        "--service", help="the traced service to read from a trace store like Jaeger"
    )
    parser.add_argument(
        "--since",
        metavar="DURATION",
        help="only runs that started in this window before now, for example 1h",
    )
    parser.add_argument(
        "--config", type=Path, help="TOML settings, default ./faultline.toml if present"
    )
    parser.add_argument(
        "--runs", action="store_true", help="also print every run, not only the report"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON; other output goes to stderr",
    )
    parser.add_argument("files", nargs="*", help="shorthand for --source file:FILE")
    parser.set_defaults(handler=run)


def fail(message: str) -> int:
    print(f"faultline find: {message}", file=sys.stderr)
    return 2


def no_traces(args: argparse.Namespace) -> str:
    return f"no traces for service {args.service} in the last {args.since}"


def run(args: argparse.Namespace) -> int:
    specs: Sequence[str] = [*args.source, *(f"file:{f}" for f in args.files)]
    if not specs:
        return fail("give --source SPEC or at least one file")
    try:
        config = load_config(args.config)
        since = None if args.since is None else parse_since(args.since)
        sources = [
            parse_source(spec, config.attributes, service=args.service, since=since)
            for spec in specs
        ]
    except ValueError as error:  # includes ConfigError
        return fail(str(error))
    out = sys.stderr if args.json else sys.stdout  # with --json, stdout is only JSON
    runs: list[Run] = []
    files = fetched = 0
    for source in sources:
        try:
            found = list(source.runs())
        except OSError as error:
            return fail(
                f"cannot read {error.filename or source.name}: {error.strerror}"
            )
        stats = source.stats()
        files += stats.files
        fetched += stats.traces
        runs += found
        if args.runs:
            print(*(show_run(r) for r in found), sep="\n", file=out)
        if stats.unreadable:
            print(
                f"{source.name}: skipped {stats.unreadable} unreadable line(s)",
                file=out,
            )
        if stats.no_spans:
            print(f"{source.name}: {stats.no_spans} line(s) held no spans", file=out)
        if isinstance(source, JaegerSource) and not stats.traces:
            print(f"{source.name}: {no_traces(args)}", file=out)
    inputs = [f"{files} file(s)"] if files or not fetched else []
    inputs += [f"{fetched} fetched trace(s)"] if fetched else []
    print(f"read {len(runs)} run(s) from {' and '.join(inputs)}", file=out)
    if files == 0 and fetched == 0:
        if any(isinstance(source, JaegerSource) for source in sources):
            return fail(no_traces(args))  # a wrong service name, most often
        return fail("no span files found")
    if not runs:
        window = f" that started in the last {args.since}" if args.since else ""
        return fail(
            f"read the input but found no runs{window}; are they OTLP GenAI spans?"
        )
    report = find(runs, config)
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(show_report(report))
    return 0
