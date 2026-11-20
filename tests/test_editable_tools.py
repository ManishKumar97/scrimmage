"""A tool in a package that is installed editable, found the way an import finds it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scrimmage_core import tool_gate

pytestmark = pytest.mark.slow

ROOT = Path(__file__).parents[1]
SETUPTOOLS = """
[project]
name = "shop"
version = "0.1"
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"
[tool.setuptools]
packages = ["shop"]
[tool.setuptools.package-dir]
"shop" = "src/shop"
"""
HATCH = """
[project]
name = "shop"
version = "0.1"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/shop"]
"""
CONFIG = """
[agent]
function = "bot:answer"
tools = ["shop.tools:get_order"]
real = ["tool:get_order"]
runs = 1
parallel = 1
max_cost_usd = "none"

[[test]]
name = "t"
question = "q"
expect = ["shipped"]
"""


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, (command, done.stdout, done.stderr)
    return done.stdout


@pytest.fixture(params=["setuptools-uv", "setuptools-pip", "hatchling-uv"])
def editable(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path]:
    """A venv with Scrimmage and a package `shop` installed editable; the package, the venv."""
    kind, _, installer = str(request.param).partition("-")
    package = tmp_path / "proj"
    (package / "src" / "shop").mkdir(parents=True)
    (package / "pyproject.toml").write_text(SETUPTOOLS if kind == "setuptools" else HATCH)
    # The package's own import writes a file, to show a lookup never runs it.
    (package / "src" / "shop" / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(tmp_path / 'imported')!r}).write_text('yes')\n"
    )
    (package / "src" / "shop" / "tools.py").write_text(
        "def get_order(order_id: str) -> str:\n    return 'shipped'\n"
    )
    venv = tmp_path / "venv"
    run(["uv", "venv", "--seed", "--python", sys.executable, str(venv)], tmp_path)
    python = str(venv / "bin" / "python")
    scrimmage = str(ROOT / "packages" / "scrimmage")
    if installer == "uv":
        install = ["uv", "pip", "install", "--python", python]
    else:
        install = [python, "-m", "pip", "install", "-q"]
    run([*install, "-e", scrimmage, "-e", str(package)], tmp_path)
    return venv, tmp_path


def clean_env(venv: Path) -> dict[str, str]:
    skip = ("SCRIMMAGE_", "VIRTUAL_ENV", "UV_", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if not k.startswith(skip)}
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env['PATH']}"
    return env


def test_an_editable_package_tool_is_found_gated_and_never_imported(
    editable: tuple[Path, Path],
) -> None:
    venv, folder = editable
    env = clean_env(venv)
    python = str(venv / "bin" / "python")
    # The lookup alone finds the module and runs none of the package.
    code = (
        "from scrimmage_core import tool_gate; "
        "print(tool_gate.module_spec('shop.tools', []) is not None, "
        "tool_gate.module_spec('shop.nope', []) is None)"
    )
    assert run([python, "-c", code], folder, env).split() == ["True", "True"]
    assert not (folder / "imported").exists(), "the lookup imported the package"

    test = folder / "test"
    test.mkdir()
    (test / "scrimmage.toml").write_text(CONFIG)
    (test / "bot.py").write_text(
        "from shop import tools\n\ndef answer(q: str) -> str:\n    return tools.get_order('1')\n"
    )
    scrimmage = str(venv / "bin" / "scrimmage")
    assert "valid" in run([scrimmage, "check", "scrimmage.toml"], test, env)
    run([scrimmage, "test", "--json", "scrimmage.toml"], test, env)
    # Exit 0 is a pass; the call went through the gate, which the record names.
    records = sorted((test / "scrimmage-runs").glob("*/run-*.jsonl"))
    text = "\n".join(r.read_text() for r in records)
    assert records and '"call": "tool:get_order"' in text, text
    assert "cannot be found" not in text


def test_a_tool_in_no_package_is_still_not_found(editable: tuple[Path, Path]) -> None:
    venv, folder = editable
    env = clean_env(venv)
    python = str(venv / "bin" / "python")
    code = "from scrimmage_core import tool_gate; print(tool_gate.module_spec('shopp.tools', []))"
    assert run([python, "-c", code], folder, env).strip() == "None"


def test_a_finder_on_meta_path_is_asked_with_its_parents_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No import: the parent's spec gives the folders the next part is searched in."""
    import importlib.abc
    import importlib.machinery
    import importlib.util

    seen: list[tuple[str, object]] = []
    mapped = tmp_path / "real"
    (mapped / "mapped_pkg").mkdir(parents=True)
    (mapped / "mapped_pkg" / "__init__.py").write_text(
        f"import pathlib\npathlib.Path({str(tmp_path / 'm')!r}).write_text('x')\n"
    )
    (mapped / "mapped_pkg" / "tools.py").write_text("")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):  # type: ignore[no-untyped-def]
            seen.append((fullname, path))
            if fullname.partition(".")[0] != "mapped_pkg":
                return None
            return importlib.machinery.PathFinder.find_spec(fullname, path or [str(mapped)])

    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    assert tool_gate.module_spec("mapped_pkg.tools", []) is not None
    assert seen[0] == ("mapped_pkg", None)
    assert seen[1] == ("mapped_pkg.tools", [str(mapped / "mapped_pkg")])
    assert not (tmp_path / "m").exists() and "mapped_pkg" not in sys.modules
