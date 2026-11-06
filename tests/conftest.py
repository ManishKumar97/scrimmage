import json
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from derail_sdk.cli import main
from fakes.openai_server import FakeOpenAI

EXAMPLE = Path(__file__).parent / "example"
# The S1 conversation: the bot asks for the order, then the stock, then answers.
ACME = ["acme_get_order", "acme_get_stock", "acme_answer"]


def pytest_sessionfinish(session: pytest.Session) -> None:
    """In CI a skipped test fails the run: a safety check that did not run proves nothing.

    A skip is fine on a laptop without Ubuntu's Python; CI must have everything.
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if os.environ.get("CI") and skipped:
        names = ", ".join(report.nodeid for report in skipped)
        print(f"\nskipped tests are not allowed in CI: {names}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def fake_openai() -> Iterator[Callable[..., FakeOpenAI]]:
    """Start a fake OpenAI API that serves the given fixtures, one per turn.

    At the end of the test it fails if the fake answered any request with an error or
    if a listed fixture was never asked for.
    """
    started: list[FakeOpenAI] = []

    def start(replies: list[str], **fixtures: str) -> FakeOpenAI:
        fake = FakeOpenAI(replies, **fixtures)
        fake.start()
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.stop()
        assert not fake.errors, (
            f"the fake OpenAI API got requests it had no reply for: {fake.errors}"
        )
        assert not fake.unused, f"fixtures listed but never asked for: {fake.unused}"


@dataclass
class Result:
    code: int
    out: str
    err: str
    root: Path

    def records(self) -> list[list[dict[str, object]]]:
        """Every run's record lines, one list per run."""
        files = sorted((self.root / "derail-runs").glob("*/*.jsonl"))
        return [[json.loads(line) for line in f.read_text().splitlines()] for f in files]

    def summaries(self) -> list[dict[str, object]]:
        return [lines[-1]["summary"] for lines in self.records()]  # type: ignore[misc]


@dataclass
class Project:
    root: Path
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch

    @property
    def toml(self) -> Path:
        return self.root / "derail.toml"

    def edit(self, old: str, new: str) -> None:
        text = self.toml.read_text()
        assert old in text, old
        self.toml.write_text(text.replace(old, new))

    def test(self, fake: FakeOpenAI | None = None) -> Result:
        if fake is not None:
            self.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
            self.monkeypatch.setenv("OPENAI_API_KEY", "test")
        self.capsys.readouterr()
        code = main(["test", str(self.toml)])
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err, self.root)


@pytest.fixture
def project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Project:
    """A copy of Acme's example agent and its derail.toml in a fresh folder."""
    root = tmp_path / "acme-bot"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__"))
    return Project(root, capsys, monkeypatch)
