"""A tool in a package that is installed editable, found the way an import finds it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.fixture(scope="module", params=["setuptools-uv", "setuptools-pip", "hatchling-uv"])
def editable(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path]:
    """A venv with Scrimmage and a package `shop` installed editable; the package, the venv."""
    kind, _, installer = str(request.param).partition("-")
    tmp_path = tmp_path_factory.mktemp(str(request.param))
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
    """One venv per layout: a name in no package is still not found."""
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
    code = "from scrimmage_core import tool_gate; print(tool_gate.module_spec('shopp.tools', []))"
    assert run([python, "-c", code], folder, env).strip() == "None"
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


FRONT_FINDER = """
import importlib, sys
class Front:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if "." in name and name.split(".")[0] == "shop":
            importlib.import_module(name.rpartition(".")[0])
        return None
sys.meta_path.insert(0, Front)
"""


def test_a_finder_that_imports_the_package_stops_the_program_before_any_call(
    editable: tuple[Path, Path],
) -> None:
    """The program path: the run ends with exit 2, names the finder, and no tool call runs."""
    venv, folder = editable
    env = clean_env(venv)
    python = str(venv / "bin" / "python")
    purelib = Path(
        run(
            [python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], folder, env
        ).strip()
    )
    (purelib / "zz_front.py").write_text(FRONT_FINDER)
    (purelib / "zz_front.pth").write_text("import zz_front\n")
    test = folder / "stopped"
    test.mkdir()
    (test / "scrimmage.toml").write_text(CONFIG)
    (test / "bot.py").write_text(
        "import shop\n\ndef answer(q: str) -> str:\n    return shop.tools.get_order('1')\n"
    )
    try:
        done = subprocess.run(
            [str(venv / "bin" / "scrimmage"), "test", "scrimmage.toml"],
            cwd=test,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        (purelib / "zz_front.pth").unlink()
        (purelib / "zz_front.py").unlink()
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "Front imported shop while Scrimmage looked up shop.tools" in out, out
    records = "\n".join(r.read_text() for r in (test / "scrimmage-runs").glob("*/run-*.jsonl"))
    assert '"action": "real"' not in records, records


def test_a_finder_that_imports_the_package_stops_the_service(tmp_path: Path) -> None:
    """The service path: the process ends with exit 1 before it serves a request."""
    mapped = tmp_path / "pkgs"
    (mapped / "svc_pkg").mkdir(parents=True)
    (mapped / "svc_pkg" / "__init__.py").write_text("")
    (mapped / "svc_pkg" / "tools.py").write_text("def get_order(order_id):\n    return 'x'\n")
    token = tmp_path / "token"
    token.write_text("a" * 64)
    token.chmod(0o600)
    code = (
        "import importlib, sys\n"
        "class Front:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'svc_pkg.tools':\n"
        "            importlib.import_module('svc_pkg')\n"
        "sys.meta_path.insert(0, Front)\n"
        "from scrimmage_core import bootstrap\n"
        "bootstrap.start_service()\n"
        "print('served')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    env.update(
        PYTHONPATH=str(mapped),
        SCRIMMAGE_SERVICE="1",
        SCRIMMAGE_TOKEN_FILE=str(token),
        SCRIMMAGE_CONTROL_URL="http://127.0.0.1:1",
        SCRIMMAGE_TOOLS="svc_pkg.tools:get_order",
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "Front imported svc_pkg while Scrimmage looked up svc_pkg.tools" in done.stderr
    assert "served" not in done.stdout
