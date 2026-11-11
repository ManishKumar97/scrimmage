"""scrimmage show: read a finished test's folder and say what happened, with no model call.

It opens files read-only and opens no socket. Every file is looked at before it is opened,
opened without following a link, and looked at again: a link, another user's file or a file
that is not a plain file is refused by name. A line it prints was redacted when it was
recorded, and is redacted again here.
"""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, cast

from scrimmage_core import results, suggest
from scrimmage_core.judge import BASELINE_FAILED
from scrimmage_core.outcomes import PREVIEW, plural
from scrimmage_core.records import RECORD_VERSION, Refused, open_owned
from scrimmage_core.redact import redact
from scrimmage_core.render import HEAD, fault_text, row_cells, table_lines

RUN_ID = re.compile(r"run-[0-9a-f]{12}")
RECORD_NAME = re.compile(r"(run-[0-9a-f]{12})\.jsonl")
FOLDER_NAME = re.compile(r"(\d{8}T\d{6}Z)-[0-9a-f]{6}")
RUNS_FOLDER = "scrimmage-runs"
MAX_LINES = 200  # timeline lines kept for one run; the rest is a count
MAX_LINE_BYTES = 8 * 1024 * 1024  # a record line over this is skipped, not parsed
SUMMARY_BYTES = 16 * 1024 * 1024  # how far back from the end a summary line is looked for
CAME_BACK = 80
LOG_TAIL = 200
LOG_READ = 64 * 1024  # the end of a .log looked at to find its last line
OTHERS = 20  # run ids listed for one row
BAD_ENDS = ("crashed", "timed out")
GATES = {"real": "real", "mock": "mock", "fault": "fault", "refused": "refused"}
ACTIONS = tuple(GATES)  # what the gate did with a call; the schema lists them
MISSES = {"real": "real (no mock matched)", "refused": "refused (no mock matched)"}
Doc = dict[str, object]


@dataclass
class Shown:
    """What scrimmage show found: its fields for --json, its text, and what went wrong."""

    code: int = 0
    records: Path | None = None
    result: Doc | None = None
    runs: list[Doc] = field(default_factory=list[Doc])
    text: list[str] = field(default_factory=list[str])
    mistakes: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])

    @property
    def fields(self) -> Doc:
        return {
            "records": None if self.records is None else str(self.records),
            "result": self.result,
            "runs": self.runs,
            "compare": None,
        }


def stamp_of(name: str) -> str | None:
    found = FOLDER_NAME.fullmatch(name)
    return None if found is None else found.group(1)


def newest(root: Path) -> tuple[Path | None, list[str]]:
    """The newest records folder under root/scrimmage-runs that holds the result of a test.

    Newest is by the UTC stamp in the folder's name. A folder with no result.json, or with
    the result of another command, does not count.
    """
    runs = root / RUNS_FOLDER
    try:
        names = os.listdir(runs)
    except OSError as exc:
        return None, [f"{runs}: cannot list it: {exc.strerror}; run scrimmage test first"]
    stamped = sorted(((stamp_of(n), n) for n in names if stamp_of(n)), reverse=True)
    for _, name in stamped:
        doc, problems = results.read(runs / name)
        if not problems and doc is not None and doc.get("command") == "test":
            return runs / name, []
    return None, [f"{runs}: holds no finished test; run scrimmage test first"]


def locate(target: Path | None, root: Path) -> tuple[Path | None, Path | None, list[str]]:
    """(records folder, result file or None, problems) for the TARGET given, or the newest."""
    if target is None:
        folder, problems = newest(root)
        return folder, None, problems
    try:
        mode = os.lstat(target).st_mode
    except OSError as exc:
        return None, None, [f"{target}: {exc.strerror}"]
    if stat.S_ISLNK(mode):
        return None, None, [f"{target}: is a symbolic link, so it is not read"]
    if stat.S_ISDIR(mode):
        return target, None, []
    if target.name == results.RESULT_FILE:
        return target.parent, target, []
    return None, None, [f"{target}: is not a records folder or a {results.RESULT_FILE}"]


def run_files(folder: Path) -> tuple[dict[str, Path], list[str]]:
    """Every run's record in the folder, by run id. The folder itself must be safe to open."""
    try:
        fd = open_owned(folder, folder=True)
    except Refused as exc:
        return {}, [str(exc)]
    except OSError as exc:
        return {}, [f"{folder}: {exc.strerror}"]
    try:
        names = os.listdir(fd)
    finally:
        os.close(fd)
    found = {m.group(1): folder / n for n in names if (m := RECORD_NAME.fullmatch(n))}
    return dict(sorted(found.items())), []


def lines_of(handle: BinaryIO):
    """The lines of a record as bytes, with a line over the cap read as b"" and skipped."""
    while raw := handle.readline(MAX_LINE_BYTES + 1):
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            while (rest := handle.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                pass
            yield b""
            continue
        yield raw


def last_lines(path: Path, want: int, window: int) -> tuple[list[bytes], int]:
    """The last `want` non-empty, complete lines of a file, read from its end, and its size.

    At most `window` bytes are read; a line cut by the window's start is not returned.
    """
    with os.fdopen(open_owned(path), "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        read = min(size, window)
        handle.seek(size - read)
        data = handle.read(read)
    parts = data.split(b"\n")
    if read < size:
        parts = parts[1:]  # the first part may start in the middle of a line
    return [p for p in parts if p.strip()][-want:], size


def summary_of(path: Path) -> Doc | None:
    """The run's summary line: the last line of its record, if it is one."""
    window = 64 * 1024
    while True:
        try:
            tail, size = last_lines(path, 1, window)
        except (Refused, OSError):
            return None
        if tail:
            try:
                line = json.loads(tail[-1])
            except ValueError:
                return None
            if isinstance(line, dict) and isinstance(cast(Doc, line).get("summary"), dict):
                return cast(Doc, line)
            return None
        if window >= size or window >= SUMMARY_BYTES:
            return None
        window *= 4


def known(line: Doc) -> bool:
    return line.get("v") == RECORD_VERSION


def started_at(line: Doc) -> float:
    try:
        return datetime.fromisoformat(str(line["start"])).timestamp()
    except (KeyError, ValueError, TypeError, OverflowError):
        return 0.0


def seq_of(line: Doc) -> int:
    value = line.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def proc_text(line: Doc) -> str:
    return json.dumps(line.get("proc"), sort_keys=True)


def came_back(line: Doc) -> str:
    error = line.get("error")
    if error:
        text = str(error)
        if line.get("action") == "fault":
            text += " (injected)"
    else:
        result = line.get("result")
        text = ""
        if isinstance(result, dict):
            text = str(cast(Doc, result).get("preview") or "")
        elif result is not None:
            text = str(result)
    text = " ".join(redact(text).split())
    return text if len(text) <= CAME_BACK else text[: CAME_BACK - 3] + "..."


def gate_of(action: str, missed: bool) -> str:
    """What the gate did with a call, as a person says it."""
    if missed and action in MISSES:
        return MISSES[action]
    return GATES.get(action, action)


def timeline(path: Path) -> tuple[list[Doc], int, int, bool]:
    """(call lines in order, lines left out, lines skipped, a bad version seen) of one record.

    Only the first MAX_LINES call lines are kept; the file is streamed, never held.
    """
    kept: list[Doc] = []
    left_out = skipped = 0
    bad_version = False
    with os.fdopen(open_owned(path), "rb") as handle:
        for raw in lines_of(handle):
            if not raw.strip():
                if raw:
                    continue
                skipped += 1
                continue
            if len(kept) >= MAX_LINES:
                left_out += b'"action"' in raw and b'"summary"' not in raw
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(line, dict):
                skipped += 1
                continue
            doc = cast(Doc, line)
            if not known(doc):
                bad_version = True
                continue
            if "summary" in doc or "action" not in doc:
                continue
            if not all(isinstance(doc.get(k), str) for k in ("action", "call")):
                skipped += 1
                continue
            kept.append(doc)
    return kept, left_out, skipped, bad_version


def ordered(lines: list[Doc]) -> list[tuple[int, Doc]]:
    """Lines by start, process and sequence; a line with a parent sits under it, indented."""
    lines = sorted(lines, key=lambda x: (started_at(x), proc_text(x), seq_of(x)))
    ids = {str(x["call_id"]) for x in lines if x.get("call_id") is not None}
    children: dict[str, list[Doc]] = {}
    roots: list[Doc] = []
    for line in lines:
        parent = line.get("parent")
        if parent is not None and str(parent) in ids:
            children.setdefault(str(parent), []).append(line)
        else:
            roots.append(line)
    out: list[tuple[int, Doc]] = []
    placed: set[int] = set()

    def place(line: Doc, depth: int) -> None:
        if id(line) in placed:
            return
        placed.add(id(line))
        out.append((depth, line))
        if line.get("call_id") is not None:
            for child in children.get(str(line["call_id"]), []):
                place(child, depth + 1)

    for root in roots:
        place(root, 0)
    for line in lines:  # a parent cycle would hide its lines; they are shown at the top
        place(line, 0)
    return out


def log_tail(folder: Path, run: str) -> Doc | None:
    path = folder / f"{run}.log"
    try:
        tail, _ = last_lines(path, 1, LOG_READ)
    except (Refused, OSError):
        return None
    text = redact(tail[-1].decode("utf-8", "replace").strip()) if tail else ""
    return {"path": str(path), "line": text[:LOG_TAIL] if text else None}


def run_doc(folder: Path, run: str, path: Path, row: Doc | None) -> tuple[Doc, list[str]]:
    """One run, for the timeline and for --json, and the notes about reading it."""
    notes: list[str] = []
    summary_line = summary_of(path)
    summary = cast(Doc, summary_line["summary"]) if summary_line is not None else None
    kept, left_out, skipped, bad = timeline(path)
    if bad:
        raise Refused(f"{path}: holds a record line of an unknown version")
    if skipped:
        notes.append(f"{run}: {plural(skipped, 'line')} could not be read, and skipped")
    if summary is None:
        notes.append(f"{run}: the run did not end: its record has no summary line")
    shown = ordered(kept)
    first = min((started_at(x) for x in kept), default=0.0)
    out_lines: list[Doc] = [
        {
            "ms": max(round((started_at(x) - first) * 1000), 0),
            "call": redact(str(x.get("call"))),
            "kind": x.get("kind"),
            "action": x.get("action"),
            "came_back": came_back(x),
            "depth": depth,
            "mock_miss": "mock_miss" in x,
        }
        for depth, x in shown
    ]
    head = summary_line if summary_line is not None else (kept[0] if kept else {})
    answer = None if summary is None else summary.get("answer")
    outcome = None if summary is None else summary.get("outcome")
    doc: Doc = {
        "run": run,
        "test": head.get("test"),
        "row": head.get("row"),
        "outcome": outcome,
        "judged": None if summary is None else summary.get("judged"),
        "left_out": None if summary is None else summary.get("left_out"),
        "broken": [] if summary is None else summary.get("broken_rules", []),
        "answer_preview": None if answer is None else redact(str(answer))[:PREVIEW],
        "lines": out_lines,
        "lines_left_out": left_out,
        "log_tail": log_tail(folder, run) if outcome in BAD_ENDS else None,
        "suggestions": [],
    }
    doc["suggestions"] = [s.doc for s in suggest.for_run(doc, kept, row)]
    return doc, notes


def run_text(doc: Doc, row: Doc | None) -> list[str]:
    """One run's timeline as lines, in the words of the judge."""
    fault = (
        ""
        if row is None or row["fault"] is None
        else f"  {row['row']}: {fault_text(cast('Doc', row['fault']))}"
    )
    if row is not None and row["fault"] is None:
        fault = f"  {row['row']}"
    elif row is None and doc["row"] is not None:
        fault = f"  {doc['row']}"
    lines = [f"{doc['run']}  {doc['test']}{fault}"]
    if doc["outcome"] is None:
        lines.append("outcome: the run did not end")
    else:
        judged = doc["judged"]
        if judged == "left out":
            how = f"left out of the rate: {doc['left_out']}"
        else:
            how = f"judged {judged} against the test's rules"
        lines.append(f"outcome: {doc['outcome']}; {how}")
    calls = cast(list[Doc], doc["lines"])
    if calls:
        rows = [
            [
                str(x["ms"]),
                "  " * int(str(x["depth"])) + str(x["call"]),
                gate_of(str(x["action"]), bool(x["mock_miss"])),
                str(x["came_back"]),
            ]
            for x in calls
        ]
        head = ["ms", "call", "gate", "came back"]
        widths = [max(len(r[i]) for r in [head, *rows]) for i in range(3)]

        def columns(r: list[str]) -> str:
            return (
                f"{r[0].rjust(widths[0])}  {r[1].ljust(widths[1])}  {r[2].ljust(widths[2])}  {r[3]}"
            )

        lines += [columns(r) for r in [head, *rows]]
    left = int(str(doc["lines_left_out"]))
    if left:
        lines.append(f"and {plural(left, 'more line')} not shown")
    if doc["answer_preview"] is not None:
        lines.append(f"answer: {doc['answer_preview']}")
    broken = cast(list[Doc], doc["broken"])
    lines += [f"broken: {b['text']}" for b in broken]
    tail = cast("Doc | None", doc["log_tail"])
    if tail is not None:
        last = tail["line"] if tail["line"] is not None else "(empty)"
        lines.append(f"log: {last}")
        lines.append(f"log file: {tail['path']}")
    lines += [str(s["text"]) for s in cast("list[Doc]", doc["suggestions"])]
    return lines


def failing_first(rows: list[Doc]) -> list[tuple[Doc, str]]:
    """For each failing row, its first failing run. A row beside a failed baseline shows no
    rate, yet its failing runs are still worth reading."""
    found: list[tuple[Doc, str]] = []
    for row in rows:
        if row["result"] not in ("fail", BASELINE_FAILED):
            continue
        details = cast("list[Doc]", row.get("run_details") or [])
        run = next((str(d["run"]) for d in details if d["judged"] == "fail"), None)
        if run is not None:
            found.append((row, run))
    return found


def table_text(doc: Doc, folder: Path) -> list[str]:
    rows = cast("list[Doc]", doc["rows"])
    runs = sum(int(str(r["runs"])) for r in rows)
    tests = int(str(doc["tests"]))
    head = (
        f"scrimmage test: {plural(tests, 'test')}, {plural(len(rows), 'row')}, "
        f"{plural(runs, 'run')}; records in {folder}"
    )
    table = [row_cells(r) for r in rows]
    names = [f"{r['test']} {r['row']}" for r in rows]
    return [
        head,
        *table_lines([HEAD, *table], names),
        "range: a 95% Wilson interval, from these runs alone",
    ]


def others_text(rows: list[Doc], shown: set[str]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        ids = [str(d["run"]) for d in cast("list[Doc]", row.get("run_details") or [])]
        ids = [i for i in ids if i not in shown]
        if not ids:
            continue
        more = f" and {len(ids) - OTHERS} more" if len(ids) > OTHERS else ""
        lines.append(f"{row['test']} {row['row']}: {', '.join(ids[:OTHERS])}{more}")
    return ["other runs, by row:", *[f"  {x}" for x in lines]] if lines else []


def grouped(summaries: dict[str, Doc | None]) -> list[str]:
    """The runs of a folder with no result file, grouped by test and row."""
    groups: dict[tuple[str, str], list[str]] = {}
    for run, line in summaries.items():
        key = ("?", "?") if line is None else (str(line.get("test")), str(line.get("row")))
        groups.setdefault(key, []).append(run)
    out: list[str] = []
    for (test, row), ids in sorted(groups.items()):
        more = f" and {len(ids) - OTHERS} more" if len(ids) > OTHERS else ""
        out.append(f"  {test} {row}: {', '.join(ids[:OTHERS])}{more}")
    return out


def build(target: Path | None, run: str | None, root: Path) -> Shown:
    """Everything scrimmage show says about TARGET, or the newest test under root."""
    shown = Shown()
    if run is not None and RUN_ID.fullmatch(run) is None:
        shown.mistakes.append(f"{run}: is not a run id; a run id is run- and 12 hex digits")
        shown.code = 2
        return shown
    folder, result_file, problems = locate(target, root)
    shown.records = folder
    if folder is None or problems:
        shown.mistakes += problems
        shown.code = 2
        return shown
    files, problems = run_files(folder)
    if problems:
        shown.mistakes += problems
        shown.code = 2
        return shown
    if run is not None and run not in files:
        shown.mistakes.append(f"{folder}: has no record of {run}")
        shown.code = 2
        return shown
    if not files:
        shown.mistakes.append(f"{folder}: holds no run records (run-<id>.jsonl)")
        shown.code = 2
        return shown
    try:
        read_into(shown, folder, result_file, run, files)
    except Refused as exc:
        shown.mistakes.append(str(exc))
        shown.code = 2
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        shown.mistakes.append(
            f"{folder}: a record or result has a shape Scrimmage does not read: {exc!r}"
        )
        shown.code = 2
    except OSError as exc:
        shown.mistakes.append(f"{folder}: {exc.strerror}")
        shown.code = 2
    return shown


def read_into(
    shown: Shown, folder: Path, result_file: Path | None, run: str | None, files: dict[str, Path]
) -> None:
    result_path = result_file or folder / results.RESULT_FILE
    if os.path.lexists(result_path):
        doc, problems = results.read(result_path)
        if problems or doc is None:
            raise Refused("; ".join(problems))
        if doc.get("command") != "test":
            raise Refused(
                f"{result_path}: is the result of scrimmage {doc.get('command')}, not test"
            )
        problems = results.shape_problems(doc, "result", full=True)
        if problems:
            raise Refused(f"{result_path}: {problems[0]}")
        shown.result = doc
    rows = cast("list[Doc]", shown.result["rows"]) if shown.result is not None else []
    by_run = {str(d["run"]): r for r in rows for d in cast("list[Doc]", r.get("run_details") or [])}
    if run is not None:
        doc, notes = run_doc(folder, run, files[run], by_run.get(run))
        shown.runs, shown.notes = [doc], notes
        shown.text = run_text(doc, by_run.get(run))
        return
    if shown.result is None:
        shown.notes.append(
            f"{results.RESULT_FILE} is not in {folder}: it is a record, a test that did not "
            "finish, or from before Scrimmage wrote one, so there is no table"
        )
        summaries = {r: summary_of(p) for r, p in files.items()}
        shown.text = [f"records in {folder}: {plural(len(files), 'run')}", *grouped(summaries)]
        chosen = [
            r
            for r, line in summaries.items()
            if line is not None and cast(Doc, line["summary"]).get("judged") == "fail"
        ]
        seen: set[tuple[str, str]] = set()
        for r in chosen:
            line = cast(Doc, summaries[r])
            key = (str(line.get("test")), str(line.get("row")))
            if key in seen:
                continue
            seen.add(key)
            doc, notes = run_doc(folder, r, files[r], None)
            shown.runs.append(doc)
            shown.notes += notes
            shown.text += ["", *run_text(doc, None)]
        return
    shown.text = table_text(shown.result, folder)
    shown_runs: set[str] = set()
    for row, first in failing_first(rows):
        if first not in files:
            shown.notes.append(f"{first}: its record is not in {folder}")
            continue
        doc, notes = run_doc(folder, first, files[first], row)
        shown.runs.append(doc)
        shown.notes += notes
        shown_runs.add(first)
        shown.text += ["", *run_text(doc, row)]
    others = others_text(rows, shown_runs)
    if others:
        shown.text += ["", *others]
