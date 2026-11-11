"""Records folders built by hand: a result.json from a report case, and run records to match.

The report cases name their runs run-baseline-1; a real run is run- and 12 hex digits, so the
ids are mapped. Nothing here starts a program.
"""

import hashlib
import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from report_cases import CASES, FOLDER
from scrimmage_core import cli, results, runner
from scrimmage_core.records import RECORD_VERSION

Doc = dict[str, object]
T0 = datetime(2026, 11, 11, 10, 15, 0, tzinfo=UTC)


def real_id(label: str) -> str:
    return "run-" + hashlib.sha1(label.encode()).hexdigest()[:12]


def renamed(value: object, names: dict[str, str]) -> object:
    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, list):
        return [renamed(v, names) for v in cast("list[object]", value)]
    if isinstance(value, dict):
        return {k: renamed(v, names) for k, v in cast(Doc, value).items()}
    return value


def case_result(name: str, folder: Path = FOLDER) -> tuple[Doc, str]:
    """The document `scrimmage test --json` would print for a report case, with real run ids,
    and the text `scrimmage test` printed for it."""
    case = CASES[name]()
    report = runner.build_report(case.config, case.results, folder, case.runner)  # type: ignore[arg-type]
    out = io.StringIO()
    runner.print_report(report, out)
    fields = {**runner.report_json(report), "compare": None}
    doc = cli.document(
        "test", report.code, "scrimmage.toml", report.every_message, "0.0.0", **fields
    )
    names = {r.run: real_id(r.run) for r in case.results}
    renamed_doc = cast(Doc, renamed(doc, names))
    renamed_doc["records"] = str(folder)
    return renamed_doc, out.getvalue().replace(str(FOLDER), str(folder))


def stamp(offset_ms: int) -> str:
    return (T0 + timedelta(milliseconds=offset_ms)).isoformat()


def call_line(run: str, test: str, row: str, seq: int, at: int, **extra: object) -> Doc:
    line: Doc = {
        "v": RECORD_VERSION,
        "run": run,
        "test": test,
        "row": row,
        "seq": seq,
        "proc": {"host": "h", "pid": 100, "started": stamp(0)},
        "call": "model:openai",
        "kind": "model",
        "action": "real",
        "args": {"sha256": "0", "bytes": 1, "preview": "{}"},
        "result": {"sha256": "0", "bytes": 1, "preview": "ok"},
        "error": None,
        "start": stamp(at),
        "ms": 5,
    }
    line.update(extra)
    return line


def summary_line(run: str, test: str, row: str, **summary: object) -> Doc:
    body: Doc = {
        "answer": "It shipped.",
        "outcome": "answered",
        "fault_fired": None,
        "calls": {},
        "incomplete": False,
        "model_calls": 1,
        "cost_usd": 0.0,
        "judged": "pass",
        "broken_rules": [],
        "left_out": None,
    }
    body.update(summary)
    return {"v": RECORD_VERSION, "run": run, "test": test, "row": row, "summary": body}


def write_lines(folder: Path, run: str, lines: list[Doc], *, end: str = "\n") -> Path:
    path = folder / f"{run}.jsonl"
    path.write_text("\n".join(json.dumps(line, sort_keys=True) for line in lines) + end)
    path.chmod(0o600)
    return path


def faulted_run(folder: Path, run: str, test: str, row: str, **summary: object) -> Path:
    """A run that asked for a tool, got an injected timeout, and answered."""
    lines = [
        call_line(
            run, test, row, 1, 0, result={"sha256": "1", "bytes": 9, "preview": "tool_calls"}
        ),
        call_line(
            run,
            test,
            row,
            2,
            412,
            call="tool:get_order",
            kind="tool",
            action="fault",
            result=None,
            error="TimeoutError: injected",
            fault={"do": "timeout", "when": "always", "n": 1},
            call_id="c1",
        ),
        call_line(run, test, row, 3, 415, result={"sha256": "2", "bytes": 9, "preview": "answer"}),
        summary_line(
            run,
            test,
            row,
            **{
                "answer": "Your order shipped on Oct 30.",
                "judged": "fail",
                "broken_rules": [{"kind": "forbid", "text": 'forbid "Oct 30" found'}],
                "fault_fired": True,
                **summary,
            },
        ),
    ]
    return write_lines(folder, run, lines)


def write_folder(
    root: Path, name: str = "20261111T101500Z-a1b2c3", case: str = "failing"
) -> tuple[Path, Doc, str]:
    """A records folder for a report case: result.json, and a record for each failing run."""
    folder = root / "scrimmage-runs" / name
    folder.mkdir(parents=True, mode=0o700)
    doc, text = case_result(case, folder)
    results.write(folder, doc)
    for row in cast("list[Doc]", doc["rows"]):
        for detail in cast("list[Doc]", row["run_details"]):
            run = str(detail["run"])
            faulted_run(
                folder,
                run,
                str(row["test"]),
                str(row["row"]),
                outcome=detail["outcome"],
                judged=detail["judged"],
                left_out=detail["left_out"],
                broken_rules=[
                    {"kind": b["kind"], "text": b["text"]}
                    for b in cast("list[Doc]", row["broken"])
                    if run in cast("list[str]", b["runs"])
                ],
            )
    if not list(folder.glob("*.jsonl")):  # a case with no run at all still has a folder to read
        faulted_run(folder, real_id("none"), "order-status", "baseline")
    os.chmod(folder, 0o700)
    return folder, doc, text
