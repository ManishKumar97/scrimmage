import sys
from pathlib import Path

import pytest

from faultline import cli, runs
from faultline.sources import FileSource, parse_source

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.jsonl"))


def printed_by_runs_module(path: Path, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["runs", str(path)])
    runs.main()
    return capsys.readouterr().out


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_find_prints_what_the_runs_module_prints(path, monkeypatch, capsys):
    expected = printed_by_runs_module(path, monkeypatch, capsys)

    assert cli.main(["find", "--source", f"file:{path}"]) == 0

    assert capsys.readouterr().out == expected


def test_positional_files_are_shorthand_for_file_sources(capsys):
    cli.main(["find", "--source", f"file:{FIXTURES[0]}"])
    long_form = capsys.readouterr().out

    cli.main(["find", str(FIXTURES[0])])

    assert capsys.readouterr().out == long_form


def test_a_directory_source_reads_every_file_in_sorted_order(capsys):
    folder = FIXTURES[0].parent

    source = FileSource(folder)

    assert [label for label, _ in source.readings()] == [str(p) for p in FIXTURES]
    assert list(source.runs()) == [r for _, rd in source.readings() for r in rd.runs]


def test_an_unknown_scheme_names_the_known_ones():
    with pytest.raises(ValueError, match="known schemes: file"):
        parse_source("kafka:topic")


def test_find_reports_an_unknown_scheme_and_a_missing_source(capsys):
    assert cli.main(["find", "--source", "kafka:x"]) == 2
    assert "known schemes" in capsys.readouterr().out
    assert cli.main(["find"]) == 2
