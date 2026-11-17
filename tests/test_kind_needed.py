"""tools/kind_needed.py: the merge gate runs the kind tests only for a branch that changes
Kubernetes code or the Kubernetes page."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "kind_needed.py"
# Built in two parts so this file does not read as a kind test file itself.
KIND_MARK = re.compile(r"\bpytest\.mark\." + r"kind\b")


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kind_needed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kind_needed = load()


def untriggered_kind_files(root: Path) -> list[str]:
    """Each file under root's tests/ that marks a kind test and that no trigger names."""
    found = [
        path.relative_to(root).as_posix()
        for path in sorted((root / "tests").rglob("*.py"))
        if KIND_MARK.search(path.read_text())
    ]
    return [path for path in found if not kind_needed.needed([path])]


def test_a_change_under_tests_k8s_needs_kind() -> None:
    paths = ["README.md", "tests/k8s/cluster.py", "tests/k8s/manifests/agent.yaml"]
    assert kind_needed.needed(paths) == paths[1:]


def test_a_change_to_the_kubernetes_page_needs_kind() -> None:
    assert kind_needed.needed(["docs/kubernetes.md"]) == ["docs/kubernetes.md"]
    assert kind_needed.needed(["docs/docker.md", "docs/services.md"]) == []


def test_a_product_only_change_does_not_need_kind() -> None:
    paths = [
        "packages/scrimmage/src/scrimmage_core/runner.py",
        "packages/scrimmage/src/scrimmage_core/records.py",
        "tests/test_secret_file.py",
        "docs/services.md",
        "images/README.md",
        "tests/k8s_notes.md",
    ]
    assert kind_needed.needed(paths) == []
    assert kind_needed.needed(["images/runner/Dockerfile"]) == ["images/runner/Dockerfile"]


def test_every_kind_test_file_is_under_a_trigger(tmp_path: Path) -> None:
    found = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "tests").glob("*.py"))
        if KIND_MARK.search(path.read_text())
    ]
    assert len(found) >= 5, found  # the scan sees the repo's kind files
    assert untriggered_kind_files(ROOT) == []
    # The positive control: a kind test file outside every trigger is reported.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_other_cluster.py").write_text(
        "import pytest\n\npytestmark = [pytest.mark." + "kind]\n"
    )
    (tmp_path / "tests" / "test_k8s_cluster.py").write_text(
        "import pytest\n\npytestmark = [pytest.mark." + "kind]\n"
    )
    assert untriggered_kind_files(tmp_path) == ["tests/test_other_cluster.py"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def run(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), base], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.slow
def test_the_script_reads_the_branch_diff_and_exits_by_it(tmp_path: Path) -> None:
    """Run as the gate runs it: against a base, on a real repository, with a move counted at
    both of its paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "tests" / "k8s").mkdir(parents=True)
    (repo / "tests" / "k8s" / "cluster.py").write_text("x = 1\n" * 20)
    (repo / "runner.py").write_text("y = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "switch", "-q", "-c", "branch")
    (repo / "runner.py").write_text("y = 2\n")
    git(repo, "commit", "-q", "-am", "product only")

    product = run(repo, "main")
    assert (product.returncode, product.stdout) == (1, "")

    git(repo, "mv", "tests/k8s/cluster.py", "cluster.py")
    git(repo, "commit", "-q", "-m", "move out of tests/k8s")
    moved = run(repo, "main")
    assert (moved.returncode, moved.stdout) == (0, "tests/k8s/cluster.py\n")

    unknown = run(repo, "no-such-base")
    assert unknown.returncode == 2 and unknown.stdout == ""
    assert "no-such-base" in unknown.stderr
