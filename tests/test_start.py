"""The starter calls the function, waits if it is async, and writes the answer to a file."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scrimmage import mocks
from scrimmage.start import main


@pytest.mark.design("start.waits_for_async")
def test_an_async_function_is_awaited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("SCRIMMAGE_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_async", "hi"]) == 0
    assert answer.read_text() == "async: hi"


@pytest.mark.design("start.answer_file")
def test_a_value_that_is_not_text_needs_a_path_and_writes_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("SCRIMMAGE_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_dict", "hi"]) == 1
    assert not answer.exists()
    assert main(["start", "starter_agents:as_dict", "hi", "reply"]) == 0
    assert answer.read_text() == "HI"


def test_a_text_enum_is_judged_by_its_value_not_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("SCRIMMAGE_ANSWER_FILE", str(answer))
    assert main(["start", "starter_agents:as_text_enum", "hi"]) == 0
    assert answer.read_text() == "still pending, no date"


def test_without_an_answer_file_the_answer_is_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SCRIMMAGE_ANSWER_FILE", raising=False)
    assert main(["start", "starter_agents:as_async", "hi"]) == 0
    assert capsys.readouterr().out == "async: hi\n"


def test_the_starter_needs_a_function_and_a_question(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 2
    assert "usage" in capsys.readouterr().err


def test_an_annotation_that_cannot_be_resolved_leaves_the_value_as_written() -> None:
    def tool() -> "NoSuchType":  # type: ignore[name-defined]  # noqa: F821
        raise AssertionError

    assert mocks.build({"a": 1}, None, tool, []) == {"a": 1}


def test_a_stray_asyncio_beside_the_agent_does_not_crash_an_async_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray, agents = tmp_path / "stray", tmp_path / "agents"
    stray.mkdir()
    agents.mkdir()
    (stray / "asyncio.py").write_text("raise RuntimeError('the stray asyncio ran')\n")
    (agents / "stray_agent.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(stray)!r})\n"  # the folder of the team's script
        "async def run(question):\n    return 'async: ' + question\n"
    )
    monkeypatch.syspath_prepend(str(agents))
    monkeypatch.setattr(sys, "path", list(sys.path))  # the agent's insert is undone after
    for name in [n for n in sys.modules if n == "asyncio" or n.startswith("asyncio.")]:
        monkeypatch.delitem(sys.modules, name)  # as in a fresh process
    answer = tmp_path / "answer.txt"
    monkeypatch.setenv("SCRIMMAGE_ANSWER_FILE", str(answer))
    try:
        assert main(["start", "stray_agent:run", "hi"]) == 0
    finally:
        sys.modules.pop("stray_agent", None)
    assert answer.read_text() == "async: hi"


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["def", "async def"])
def test_a_stray_asyncio_in_the_working_folder_never_runs(tmp_path: Path, kind: str) -> None:
    # python -m puts the working folder first, so this is where a team's asyncio.py sits.
    (tmp_path / "asyncio.py").write_text("raise RuntimeError('the stray asyncio ran')\n")
    (tmp_path / "an_agent.py").write_text(f"{kind} run(question):\n    return 'ok: ' + question\n")
    answer = tmp_path / "answer.txt"
    env = {**os.environ, "SCRIMMAGE_ANSWER_FILE": str(answer)}
    env.pop("SCRIMMAGE_TOKEN", None)
    done = subprocess.run(
        [sys.executable, "-m", "scrimmage.start", "an_agent:run", "hi"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "the stray asyncio ran" not in done.stderr
    assert answer.read_text() == "ok: hi"
