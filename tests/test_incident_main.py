import pytest

from faultline.incidents.__main__ import main


def test_run_needs_an_explicit_results_file(capsys):
    # A dated default would start over in a new empty file when a resume crossed midnight.
    with pytest.raises(SystemExit) as stop:
        main(["run", "--trials", "1"])

    assert stop.value.code == 2
    assert "--out" in capsys.readouterr().err
