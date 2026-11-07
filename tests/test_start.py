"""The starter calls the function, waits if it is async, and writes the answer to a file."""

from pathlib import Path

import pytest

from scrimmage_core import mocks
from scrimmage_core.start import main


@pytest.mark.design("start.waits_for_async")
def test_an_async_function_is_awaited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("DERAIL_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_async", "hi"]) == 0
    assert answer.read_text() == "async: hi"


@pytest.mark.design("start.answer_file")
def test_a_value_that_is_not_text_needs_a_path_and_writes_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("DERAIL_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_dict", "hi"]) == 1
    assert not answer.exists()
    assert main(["start", "starter_agents:as_dict", "hi", "reply"]) == 0
    assert answer.read_text() == "HI"


def test_a_text_enum_is_judged_by_its_value_not_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("DERAIL_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_text_enum", "hi"]) == 0
    assert answer.read_text() == "still pending, no date"


def test_without_an_answer_file_the_answer_is_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DERAIL_ANSWER_FILE", raising=False)
    assert main(["start", "starter_agents:as_async", "hi"]) == 0
    assert capsys.readouterr().out == "async: hi\n"


def test_the_starter_needs_a_function_and_a_question(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 2
    assert "usage" in capsys.readouterr().err


def test_an_annotation_that_cannot_be_resolved_leaves_the_value_as_written() -> None:
    def tool() -> "NoSuchType":  # type: ignore[name-defined]  # noqa: F821
        raise AssertionError

    assert mocks.build({"a": 1}, None, tool, []) == {"a": 1}
