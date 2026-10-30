import pytest

from faultline_lab.incidents.__main__ import main


def test_run_needs_an_explicit_results_file(capsys):
    # A dated default would start over in a new empty file when a resume crossed midnight.
    with pytest.raises(SystemExit) as stop:
        main(["run", "--trials", "1"])

    assert stop.value.code == 2
    assert "--out" in capsys.readouterr().err


def test_summary_needs_an_explicit_file(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["summary"])

    assert stop.value.code == 2
    assert "--file" in capsys.readouterr().err


def test_summary_with_a_missing_file_says_so(tmp_path, capsys):
    assert main(["summary", "--file", str(tmp_path / "none.jsonl")]) == 1
    assert "no results file" in capsys.readouterr().err
