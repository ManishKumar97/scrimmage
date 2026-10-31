import sys
from pathlib import Path

import pytest
from spans import spans_of, write_spans, write_trace

from faultline import cli, runs
from faultline.runs import read_runs, show_run
from faultline.sources import FileSource, SourceStats, parse_source

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.jsonl"))


def run_lines_of_runs_module(path: Path, monkeypatch, capsys) -> str:
    """What `python -m faultline.runs` prints, without its per-file header and notes."""
    monkeypatch.setattr(sys, "argv", ["runs", str(path)])
    runs.main()
    lines = capsys.readouterr().out.splitlines()
    return "\n".join(line for line in lines if not line.startswith(f"{path}:"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_find_prints_the_runs_the_runs_module_prints(path, monkeypatch, capsys):
    expected = run_lines_of_runs_module(path, monkeypatch, capsys)
    found = len(read_runs(path).runs)

    code = cli.main(["find", "--runs", "--source", f"file:{path}"])

    assert code == (0 if found else 2)  # no run at all is a failure
    out = capsys.readouterr().out
    assert out.startswith(expected + "\n" if expected else "")


def test_positional_files_are_shorthand_for_file_sources(capsys):
    cli.main(["find", "--runs", "--source", f"file:{FIXTURES[0]}"])
    long_form = capsys.readouterr().out

    cli.main(["find", "--runs", str(FIXTURES[0])])

    assert capsys.readouterr().out == long_form


def test_a_directory_source_reads_every_jsonl_file():
    source = FileSource(FIXTURES[0].parent)

    found = list(source.runs())

    assert source.stats().files == len(FIXTURES)
    expected = [r for path in FIXTURES for r in read_runs(path).runs]
    assert sorted(r.trace_id for r in found) == sorted(r.trace_id for r in expected)


def test_stats_are_filled_once_the_runs_are_consumed(tmp_path):
    path = write_trace(tmp_path / "a.jsonl", {"calls": []})
    with path.open("a") as file:
        file.write("not json\n{}\n")
    source = FileSource(path)

    assert source.stats() == SourceStats()
    list(source.runs())

    assert source.stats() == SourceStats(files=1, unreadable=1, no_spans=1)


def test_one_trace_split_across_two_files_is_one_run(tmp_path):
    # Spans in order: root, chat 1, its tool call, chat 2, its tool call.
    spans = spans_of([{"calls": [{"id": "a"}]}, {"calls": [{"id": "b"}]}])
    write_spans(tmp_path / "1.jsonl", spans[:3])
    write_spans(tmp_path / "2.jsonl", spans[3:])

    [run] = FileSource(tmp_path).runs()

    assert [s.number for s in run.steps] == [1, 2]
    assert [c.call_id for c in run.steps[0].tool_calls] == ["a"]
    assert [c.call_id for c in run.steps[1].tool_calls] == ["b"]


def test_an_unknown_scheme_names_the_known_ones():
    with pytest.raises(ValueError, match="known schemes: file"):
        parse_source("kafka:topic")


def test_find_reports_an_unknown_scheme_and_a_missing_source_on_stderr(capsys):
    assert cli.main(["find", "--source", "kafka:x"]) == 2
    captured = capsys.readouterr()
    assert "known schemes" in captured.err
    assert captured.out == ""
    assert cli.main(["find"]) == 2
    assert "faultline find:" in capsys.readouterr().err


def test_find_always_says_how_many_runs_and_files_it_read(tmp_path, capsys):
    write_trace(tmp_path / "a.jsonl", {"calls": []})
    write_trace(tmp_path / "b.jsonl", {"calls": []}, trace="t2")

    assert cli.main(["find", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "read 2 run(s) from 2 file(s)",
        "Faultline find: 2 runs, 0 with faults, 0 faults, 0 findings",
    ]
    assert "No faults found." in lines


def test_a_directory_with_no_span_files_is_an_error(tmp_path, capsys):
    assert cli.main(["find", str(tmp_path)]) == 2

    captured = capsys.readouterr()
    assert "read 0 run(s) from 0 file(s)" in captured.out
    assert "no span files found" in captured.err


def test_files_that_hold_no_runs_are_an_error(tmp_path, capsys):
    path = tmp_path / "other.jsonl"
    path.write_text('{"hello": "world"}\n')

    assert cli.main(["find", str(path)]) == 2

    captured = capsys.readouterr()
    assert "read 0 run(s) from 1 file(s)" in captured.out
    assert "1 line(s) held no spans" in captured.out
    assert "found no runs" in captured.err


def test_a_missing_path_is_one_line_on_stderr_and_no_traceback(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"

    assert cli.main(["find", str(missing)]) == 2

    captured = capsys.readouterr()
    assert captured.err == (
        f"faultline find: cannot read {missing}: No such file or directory\n"
    )
    assert captured.out == ""


def test_a_file_that_cannot_be_opened_is_reported_the_same_way(tmp_path, capsys):
    path = write_trace(tmp_path / "a.jsonl", {"calls": []})
    path.chmod(0)
    try:
        code = cli.main(["find", str(path)])
    finally:
        path.chmod(0o644)
    if code == 0:
        pytest.skip("file modes are not enforced for this user")

    assert code == 2
    assert f"cannot read {path}: Permission denied" in capsys.readouterr().err


def test_unreadable_lines_are_noted_even_without_the_runs_flag(tmp_path, capsys):
    path = write_trace(tmp_path / "a.jsonl", {"calls": []})
    with path.open("a") as file:
        file.write("{cut\n")

    assert cli.main(["find", str(path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert f"file:{path}: skipped 1 unreadable line(s)" in lines
    assert not any(line.startswith("run ") for line in lines)


def test_runs_flag_prints_each_run_in_the_runs_module_format(tmp_path, capsys):
    path = write_trace(tmp_path / "a.jsonl", {"calls": []})
    [run] = read_runs(path).runs

    cli.main(["find", "--runs", str(path)])

    assert show_run(run) in capsys.readouterr().out
