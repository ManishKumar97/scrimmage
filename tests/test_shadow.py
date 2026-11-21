"""A team's own module named scrimmage must not stand in for Scrimmage, or run ungated."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SIDE_EFFECT = 'open("SIDE_EFFECT", "w").write("ran")\n'


def project(tmp_path: Path) -> Path:
    """A function agent that needs no model, and a scrimmage.py in its folder."""
    root = tmp_path / "team"
    root.mkdir()
    (root / "plain.py").write_text('def run(q):\n    return "hello " + q\n')
    (root / "scrimmage.py").write_text(SIDE_EFFECT)
    (root / "scrimmage.toml").write_text(
        '[agent]\nfunction = "plain:run"\nmax_cost_usd = "none"\n\n'
        '[[test]]\nname = "t"\nquestion = "hi"\nexpect = ["hello"]\n'
    )
    return root


def scrimmage(root: Path, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    """scrimmage COMMAND in root, with -P so that the test's own folder never shadows it."""
    clean = {
        k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_") and k != "PYTHONPATH"
    }
    return subprocess.run(
        [sys.executable, "-P", "-m", "scrimmage.cli", command, "scrimmage.toml"],
        cwd=root, env={**clean, **env}, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


@pytest.mark.slow
def test_a_scrimmage_py_in_the_agents_folder_is_not_run(tmp_path: Path) -> None:
    root = project(tmp_path)
    done = scrimmage(root, "test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (root / "SIDE_EFFECT").exists(), "the team's scrimmage.py ran"
