"""scrimmage show: what happened in a finished test, read from its records folder."""

import io
import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from report_cases import CASES
from scrimmage_core import cli, results, show
from scrimmage_core.runner import report
from show_cases import call_line, faulted_run, real_id, summary_line, write_folder, write_lines

pytestmark = pytest.mark.usefixtures("signals_kept")
Doc = dict[str, object]
Fake = Callable[..., FakeOpenAI]
BASELINE_FAIL = real_id("run-baseline-1")
FAULT_CRASH = real_id("run-fault1-0")


class Out:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> Doc:
        return cast(Doc, json.loads(self.out))


def shown(capsys: pytest.CaptureFixture[str], *args: str) -> Out:
    capsys.readouterr()
    code = cli.main(["show", *args])
    captured = capsys.readouterr()
    return Out(code, captured.out, captured.err)


def at(seq: int, ms: int, **extra: object) -> Doc:
    """A call line of the baseline's failing run, `ms` after its start."""
    return call_line(BASELINE_FAIL, "order-status", "baseline", seq, ms, **extra)


def test_a_failing_row_shows_its_first_failing_runs_timeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, doc, _ = write_folder(tmp_path)
    done = shown(capsys, str(folder))
    assert done.code == 0 and done.err == ""
    lines = done.out.splitlines()
    assert lines[0].startswith("scrimmage test: 1 test, 2 rows, 6 runs; records in ")
    # The baseline's first failing run, and the fault row's first failing run.
    heads = [line for line in lines if line.startswith("run-")]
    assert heads == [
        f"{BASELINE_FAIL}  order-status  baseline",
        f"{FAULT_CRASH}  order-status  fault 1: tool:get_order timeout",
    ]
    assert "outcome: answered; judged fail against the test's rules" in lines
    assert "outcome: crashed; judged fail against the test's rules" in lines
    first = lines.index(heads[0])
    assert lines[first + 2].split() == ["ms", "call", "gate", "came", "back"]
    assert lines[first + 3].split()[:3] == ["0", "model:openai", "real"]
    assert lines[first + 4].split()[:4] == ["412", "tool:get_order", "fault", "TimeoutError:"]
    assert "TimeoutError: injected (injected)" in lines[first + 4]
    assert "answer: Your order shipped on Oct 30." in lines
    # The other runs of each row are named, not shown.
    assert "other runs, by row:" in lines
    assert any(line.startswith("  order-status baseline: run-") for line in lines)
    assert BASELINE_FAIL not in lines[lines.index("other runs, by row:") :][1]
    as_json = shown(capsys, str(folder), "--json")
    runs = cast("list[Doc]", as_json.doc["runs"])
    assert [r["run"] for r in runs] == [BASELINE_FAIL, FAULT_CRASH]
    assert as_json.doc["result"] == doc and as_json.doc["records"] == str(folder)
    assert as_json.doc["status"] == "shown" and as_json.doc["compare"] is None
    first_run = runs[0]
    assert first_run["judged"] == "fail" and first_run["outcome"] == "answered"
    assert cast("list[Doc]", first_run["lines"])[1] == {
        "ms": 412,
        "call": "tool:get_order",
        "kind": "tool",
        "action": "fault",
        "came_back": "TimeoutError: injected (injected)",
        "depth": 0,
        "mock_miss": False,
    }
    with_run = shown(capsys, str(folder), "--run", BASELINE_FAIL)  # that run alone, no table
    assert with_run.out.splitlines()[0].startswith(f"{BASELINE_FAIL}  order-status")
    assert "scrimmage test:" not in with_run.out


def test_a_fault_a_mock_a_miss_and_a_nested_web_call_each_show_their_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    run = BASELINE_FAIL
    lines = [
        at(1, 0),
        at(
            2,
            100,
            call="tool:get_stock",
            kind="tool",
            action="mock",
            mock=0,
            call_id="m1",
        ),
        at(
            3,
            200,
            call="tool:get_order",
            kind="tool",
            action="real",
            call_id="t1",
        ),
        at(
            5,
            250,
            call="http:orders.acme.internal",
            kind="http",
            action="real",
            parent="t1",
            call_id="w1",
        ),
        at(
            4,
            220,
            call="tool:get_order",
            kind="tool",
            action="refused",
            result=None,
            parent="m1",
        ),
        summary_line(run, "order-status", "baseline", judged="fail"),
    ]
    write_lines(folder, run, lines)
    done = shown(capsys, str(folder), "--run", run, "--json")
    timeline = cast("list[Doc]", cast("list[Doc]", done.doc["runs"])[0]["lines"])
    assert [(x["call"], x["action"], x["depth"]) for x in timeline] == [
        ("model:openai", "real", 0),
        ("tool:get_stock", "mock", 0),
        ("tool:get_order", "refused", 1),  # under the mock call it names as its parent
        ("tool:get_order", "real", 0),
        ("http:orders.acme.internal", "real", 1),  # a web call inside a tool, under the tool
    ]
    text = shown(capsys, str(folder), "--run", run).out.splitlines()
    body = [line for line in text if "tool:" in line or "http:" in line or "model:" in line]
    assert any(
        line.lstrip().split()[1].startswith("tool:get_stock") and " mock " in line for line in body
    )
    assert any("  http:orders.acme.internal" in line and " real " in line for line in body)
    assert any("    tool:get_order" in line and "refused" in line for line in body)
    assert any(line.rstrip().endswith("ok") for line in body)


def test_a_miss_says_real_or_refused_by_its_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    run = BASELINE_FAIL
    miss = {"reason": "arguments", "seen": "{}", "method": None, "path": None, "nearest": None}
    lines = [
        at(
            1,
            0,
            call="tool:get_order",
            kind="tool",
            action="real",
            mock_miss=miss,
        ),
        at(
            2,
            10,
            call="tool:get_stock",
            kind="tool",
            action="refused",
            result=None,
            mock_miss=miss,
        ),
        summary_line(run, "order-status", "baseline", judged="fail"),
    ]
    write_lines(folder, run, lines)
    text = shown(capsys, str(folder), "--run", run).out
    assert "real (no mock matched)" in text and "refused (no mock matched)" in text
    done = shown(capsys, str(folder), "--run", run, "--json")
    flags = [
        x["mock_miss"] for x in cast("list[Doc]", cast("list[Doc]", done.doc["runs"])[0]["lines"])
    ]
    assert flags == [True, True]


def test_a_crashed_run_shows_its_logs_last_line_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    secret = "sk-" + "a1b2c3d4" * 4
    log = folder / f"{FAULT_CRASH}.log"
    log.write_text(
        f"starting\nTraceback (most recent call last):\nKeyError: {secret} " + "x" * 400 + "\n\n"
    )
    done = shown(capsys, str(folder), "--run", FAULT_CRASH)
    tail = [line for line in done.out.splitlines() if line.startswith("log: ")]
    assert len(tail) == 1 and secret not in done.out
    assert tail[0].startswith("log: KeyError: [secret removed] xxx") and len(tail[0]) == 5 + 200
    assert f"log file: {log}" in done.out
    other = shown(capsys, str(folder), "--run", BASELINE_FAIL)  # an answered run shows no log
    assert "log:" not in other.out
    as_json = shown(capsys, str(folder), "--run", FAULT_CRASH, "--json")
    tail_doc = cast("Doc", cast("list[Doc]", as_json.doc["runs"])[0]["log_tail"])
    assert tail_doc["path"] == str(log) and secret not in str(tail_doc["line"])


def test_show_never_says_wrong(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder, _, _ = write_folder(tmp_path)
    for args in ([str(folder)], [str(folder), "--run", FAULT_CRASH], [str(folder), "--json"]):
        done = shown(capsys, *args)
        said = (done.out + done.err).replace(str(tmp_path), "")  # the test's own folder name
        assert "wrong" not in said.lower(), args
    assert "judged fail against the test's rules" in shown(capsys, str(folder)).out


@pytest.mark.design("suggest.in_show")
def test_a_failing_run_shows_its_suggestion_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    text = shown(capsys, str(folder)).out.splitlines()
    mine = [line for line in text if line.startswith("suggestion (a heuristic):")]
    assert len(mine) == 2  # one for each failing row's first failing run, none for the rest
    assert mine[0].startswith("suggestion (a heuristic): the baseline fails the test's rules")
    assert "after an injected failure in tool:get_order" in mine[1]
    as_json = shown(capsys, str(folder), "--json")
    runs = {str(r["run"]): r for r in cast("list[Doc]", as_json.doc["runs"])}
    baseline = cast("list[Doc]", runs[BASELINE_FAIL]["suggestions"])
    crashed = cast("list[Doc]", runs[FAULT_CRASH]["suggestions"])
    assert [s["code"] for s in baseline] == ["rule_on_baseline"]
    assert [s["code"] for s in crashed] == ["crash_after_fault"]
    assert all(s["heuristic"] is True for s in (*baseline, *crashed))
    # The evidence is the fault line: seq 2 of the run's process.
    proc = at(0, 0)["proc"]
    assert crashed[0]["evidence"] == [{"seq": 2, "proc": proc}]
    one = shown(capsys, str(folder), "--run", FAULT_CRASH)  # the same suggestion with --run
    assert any(line.startswith("suggestion (a heuristic):") for line in one.out.splitlines())


def printed_table(text: str) -> list[str]:
    """The lines of a test's text from its table through the "range:" line."""
    lines = text.splitlines()
    return lines[1 : lines.index("range: a 95% Wilson interval, from these runs alone") + 1]


@pytest.mark.design("json.same_as_table")
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_table_equals_what_test_printed(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, text = write_folder(tmp_path, case=name)
    done = shown(capsys, str(folder))
    assert done.code == 0, done.err
    expected = printed_table(text)
    if name == "notes_and_warnings":  # a warning is printed above the table, as the test does
        expected = [line for line in expected if not line.startswith("warning: ")]
    lines = done.out.splitlines()
    shown_table = lines[1 : lines.index("range: a 95% Wilson interval, from these runs alone") + 1]
    assert shown_table == expected
    # And it is the table the runner's own report prints.
    case = CASES[name]()
    out = io.StringIO()
    report(case.config, case.results, folder, out, case.runner)  # type: ignore[arg-type]
    assert [x for x in printed_table(out.getvalue()) if not x.startswith("warning: ")] == expected


def test_newest_skips_folders_without_a_test_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    older, _, _ = write_folder(tmp_path, "20261110T090000Z-aaaaaa")
    middle, _, _ = write_folder(tmp_path, "20261111T090000Z-bbbbbb")
    newest = tmp_path / "scrimmage-runs" / "20261111T120000Z-cccccc"  # no result.json
    newest.mkdir(mode=0o700)
    faulted_run(newest, real_id("x"), "order-status", "baseline")
    recorded = tmp_path / "scrimmage-runs" / "20261111T130000Z-dddddd"  # another command's
    recorded.mkdir(mode=0o700)
    results.write(recorded, {"v": 1, "command": "record"})
    faulted_run(recorded, real_id("y"), "order-status", "baseline")
    monkeypatch.chdir(tmp_path)
    done = shown(capsys, "--json")
    assert (
        done.code == 0 and done.doc["records"] == str(middle) and done.doc["config"] == str(middle)
    )
    assert older != middle
    monkeypatch.chdir(tmp_path / "scrimmage-runs")  # no scrimmage-runs here
    missing = shown(capsys)
    assert missing.code == 2 and "cannot list" in missing.err


def test_a_folder_without_a_result_file_shows_its_runs_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "scrimmage-runs" / "20261111T101500Z-a1b2c3"
    folder.mkdir(parents=True, mode=0o700)
    first, second, third = real_id("one"), real_id("two"), real_id("three")
    faulted_run(folder, first, "order-status", "baseline")
    faulted_run(folder, second, "order-status", "baseline", judged="pass", broken_rules=[])
    faulted_run(folder, third, "refunds", "fault 1")
    done = shown(capsys, str(folder))
    assert done.code == 0, done.err
    assert f"order-status baseline: {', '.join(sorted([first, second]))}" in done.out
    assert f"refunds fault 1: {third}" in done.out
    assert "note: result.json is not in" in done.out and "so there is no table" in done.out
    as_json = shown(capsys, str(folder), "--json")
    assert as_json.doc["result"] is None and as_json.doc["status"] == "shown"
    assert [r["run"] for r in cast("list[Doc]", as_json.doc["runs"])] == sorted([first, third])
    notes = [n["message"] for n in cast("list[Doc]", as_json.doc["notes"])]
    assert len(notes) == 1 and "no table" in str(notes[0])


def test_a_cut_record_shows_what_was_read_and_says_the_run_did_not_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    run = BASELINE_FAIL
    lines = [
        at(1, 0),
        at(2, 30),
    ]
    path = write_lines(folder, run, lines, end='\n{"v": 1, "run": "cut')  # killed mid-line
    done = shown(capsys, str(folder), "--run", run)
    assert done.code == 0 and "outcome: the run did not end" in done.out
    assert "model:openai" in done.out
    assert f"note: {run}: the run did not end: its record has no summary line" in done.out
    assert "could not be read" in done.out  # the cut line is skipped and counted
    big = [call_line(run, "order-status", "baseline", n, n) for n in range(250)]
    write_lines(folder, run, [*big, summary_line(run, "order-status", "baseline")])
    capped = shown(capsys, str(folder), "--run", run, "--json")
    doc = cast("Doc", cast("list[Doc]", capped.doc["runs"])[0])
    assert len(cast("list[Doc]", doc["lines"])) == show.MAX_LINES and doc["lines_left_out"] == 50
    assert "and 50 more lines not shown" in shown(capsys, str(folder), "--run", run).out
    assert path.exists()


def test_a_bad_or_missing_run_id_is_a_mistake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    for bad in (
        "run-3f9a1c40b2e1/../x",  # re.match would take the front of this
        "../run-3f9a1c40b2e1",
        "run-3F9A1C40B2E1",
        "run-3f9a1c40b2e",
        "x",
    ):
        done = shown(capsys, str(folder), "--run", bad)
        assert done.code == 2 and "is not a run id" in done.err, bad
    absent = shown(capsys, str(folder), "--run", "run-000000000000")
    assert absent.code == 2 and "has no record of run-000000000000" in absent.err
    as_json = shown(capsys, str(folder), "--run", "run-000000000000", "--json")
    assert as_json.doc["status"] == "mistake" and as_json.doc["exit"] == 2
    assert shown(capsys, str(tmp_path / "nowhere")).code == 2
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    assert "no run records" in shown(capsys, str(empty)).err
    assert shown(capsys, str(folder / "run-x.log")).code == 2  # a file that is not result.json
    (folder / "run-aaaaaaaaaaaa.jsonl").write_text(json.dumps({"v": 9}) + "\n")
    unknown = shown(capsys, str(folder), "--run", "run-aaaaaaaaaaaa")
    assert unknown.code == 2 and "unknown version" in unknown.err


def test_a_symlinked_target_record_log_or_result_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(folder, target_is_directory=True)
    done = shown(capsys, str(alias))
    assert done.code == 2 and "symbolic link" in done.err
    # A record that is a link.
    victim = folder / f"{BASELINE_FAIL}.jsonl"
    saved = victim.read_text()
    victim.unlink()
    victim.symlink_to(tmp_path / "elsewhere.jsonl")
    (tmp_path / "elsewhere.jsonl").write_text(saved)
    done = shown(capsys, str(folder))
    assert done.code == 2 and "symbolic link" in done.err and BASELINE_FAIL in done.err
    victim.unlink()
    victim.write_text(saved)
    # A log that is a link is not read: the crash shows no log line.
    log = folder / f"{FAULT_CRASH}.log"
    (tmp_path / "secret.log").write_text("the owner's other file\n")
    log.symlink_to(tmp_path / "secret.log")
    done = shown(capsys, str(folder), "--run", FAULT_CRASH, "--json")
    assert "the owner's other file" not in done.out
    assert cast("list[Doc]", done.doc["runs"])[0]["log_tail"] is None
    # A result that is a link.
    result = folder / results.RESULT_FILE
    body = result.read_text()
    result.unlink()
    (tmp_path / "elsewhere.json").write_text(body)
    result.symlink_to(tmp_path / "elsewhere.json")
    done = shown(capsys, str(folder))
    assert done.code == 2 and "symbolic link" in done.err
    done = shown(capsys, str(result))
    assert done.code == 2 and "symbolic link" in done.err


def snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for root in roots:
        for path in [root, *root.rglob("*")]:
            info = path.lstat()
            found[str(path)] = (info.st_mtime_ns, info.st_mode)
    return found


def test_show_writes_nothing_and_opens_no_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, _, _ = write_folder(tmp_path)
    before = snapshot(tmp_path)

    def no_socket(*_: object, **__: object) -> None:
        raise AssertionError("show opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.chdir(tmp_path)
    for args in ([str(folder)], [], ["--json"], [str(folder), "--run", FAULT_CRASH]):
        assert shown(capsys, *args).code == 0, args
    assert snapshot(tmp_path) == before
    opened: list[str] = []
    real_open = os.open

    def watching(path: str, flags: int, *rest: int, **kw: object) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            opened.append(str(path))
        return real_open(path, flags, *rest, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", watching)
    shown(capsys, str(folder))
    assert opened == []


def test_show_redacts_a_secret_that_reached_a_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    secret = "sk-" + "z9y8x7w6" * 4
    run = BASELINE_FAIL
    lines = [
        at(
            1,
            0,
            result={"sha256": "0", "bytes": 1, "preview": f"key {secret}"},
        ),
        summary_line(run, "order-status", "baseline", answer=f"{secret} shipped", judged="fail"),
    ]
    write_lines(folder, run, lines)
    for args in (["--run", run], ["--run", run, "--json"]):
        done = shown(capsys, str(folder), *args)
        assert secret not in done.out and "[secret removed]" in done.out, args


@pytest.mark.slow
def test_a_real_test_leaves_a_folder_show_can_read(
    project: Project, fake_openai: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    result = project.test(fake_openai(ACME))
    assert result.code in (0, 1), result.out + result.err
    folder = max((project.root / "scrimmage-runs").iterdir())
    done = shown(capsys, str(folder))
    assert done.code == 0 and "order-status" in done.out
    table = [line for line in result.out.splitlines()[1:] if line]
    assert "".join(table[:3]) in "".join(done.out.splitlines()[1:])


def test_a_control_character_in_a_record_is_shown_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, _, _ = write_folder(tmp_path)
    run = BASELINE_FAIL
    preview = {"sha256": "0", "bytes": 1, "preview": "a\rb\x07"}
    lines = [
        at(1, 0, call="tool:\x1b[2Jget", result=preview),
        summary_line(run, "order-status", "baseline", answer="\x1b]0;t\x07done", judged="fail"),
    ]
    write_lines(folder, run, lines)
    done = shown(capsys, str(folder), "--run", run)
    assert not any(c in done.out for c in "\x1b\x07\r")
    assert "\\x1b[2Jget" in done.out and "a b\\x07" in done.out
    assert "\\x1b]0;t\\x07done" in done.out


def test_well_formed_json_of_the_wrong_shape_is_never_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, doc, _ = write_folder(tmp_path)
    run = BASELINE_FAIL
    path = folder / f"{run}.jsonl"
    good = path.read_text().splitlines()
    odd = [
        "[1, 2]",
        "3",
        '{"v": 1, "action": 5, "call": []}',
        '{"v": 1, "action": "real", "call": "c", "seq": "x", "start": 7}',
    ]
    path.write_text("\n".join([*good[:1], *odd, *good[1:-1], '{"v": 1, "summary": "text"}']) + "\n")
    done = shown(capsys, str(folder), "--run", run)
    assert done.code == 0, done.err
    assert "could not be read" in done.out and "did not end" in done.out
    result = folder / results.RESULT_FILE
    first = cast("list[Doc]", doc["rows"])[0]
    for rows in ([{"test": "x"}], ["x"], [{**first, "passed": "9"}]):
        result.write_text(json.dumps({**doc, "rows": rows}))
        bad = shown(capsys, str(folder), "--json")
        assert bad.code == 2 and bad.doc["status"] == "mistake", rows
        assert "is damaged" in str(bad.doc["mistakes"]), rows
    result.write_text(json.dumps({**doc, "tests": "many"}))
    assert shown(capsys, str(folder)).code == 2
