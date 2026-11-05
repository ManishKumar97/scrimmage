"""Getting in before the agent starts: the startup file, the token switch, chaining."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from faultline_sdk.control import ControlServer
from faultline_sdk.records import RecordStore
from faultline_sdk.run_command import BOOT, main

TOKEN = "d" * 64
UBUNTU_PYTHON = Path("/usr/bin/python3.12")
APPORT = Path("/usr/lib/python3.12/sitecustomize.py")
GATES_LOADED = (
    "import sys; print('faultline_sdk' in sys.modules, "
    "any(type(f).__name__ == 'ToolFinder' for f in sys.meta_path))"
)


def python(
    code: str, env: dict[str, str], cwd: Path | None = None, exe: str = sys.executable
) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FAULTLINE_")}
    return subprocess.run(
        [exe, "-c", code],
        env={**clean, **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlServer]:
    server = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    server.start()
    yield server
    server.stop()


def plan(**changes: object) -> dict[str, object]:
    return {
        "v": 1, "run": "run-1", "row": "baseline", "mocks": [], "faults": [],
        "tools": [], "real": [], "time_limit_s": 60, **changes,
    }  # fmt: skip


def joined(control: ControlServer, **extra: str) -> dict[str, str]:
    return {
        "PYTHONPATH": str(BOOT),
        "FAULTLINE_TOKEN": TOKEN,
        "FAULTLINE_CONTROL_URL": control.url,
        "FAULTLINE_RUN": "run-1",
        "FAULTLINE_ALLOW_IMPORTS": "acme.",
        **extra,
    }


def test_the_startup_folder_holds_exactly_one_file() -> None:
    assert sorted(p.name for p in BOOT.iterdir() if p.name != "__pycache__") == ["sitecustomize.py"]


@pytest.mark.design("boot.off_without_token")
def test_without_the_token_nothing_is_imported_or_patched() -> None:
    result = python(GATES_LOADED, {"PYTHONPATH": str(BOOT)})
    assert (result.stdout.strip(), result.stderr) == ("False False", "")


@pytest.mark.design("boot.import_never_switches_on")
def test_importing_the_sdk_never_switches_gates_on() -> None:
    result = python("import faultline_sdk.tool_gate, faultline_sdk.bootstrap; " + GATES_LOADED, {})
    assert result.stdout.strip() == "True False"


@pytest.mark.design("boot.chains_hidden_sitecustomize")
def test_the_hidden_sitecustomize_still_runs(tmp_path: Path) -> None:
    team = tmp_path / "team"
    team.mkdir()
    marker = tmp_path / "ran"
    (team / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('yes')\n")
    result = python("print('agent')", {"PYTHONPATH": f"{BOOT}{os.pathsep}{team}"})
    assert result.stdout.strip() == "agent" and marker.read_text() == "yes"


@pytest.mark.design("boot.chains_apport")
@pytest.mark.skipif(
    not APPORT.exists(), reason="needs Ubuntu's Python and its apport sitecustomize"
)
def test_ubuntus_apport_hook_still_installs() -> None:
    code = "import sys; print(sys.excepthook.__name__)"
    plain = python(code, {}, exe=str(UBUNTU_PYTHON)).stdout.strip()
    behind = python(code, {"PYTHONPATH": str(BOOT)}, exe=str(UBUNTU_PYTHON)).stdout.strip()
    assert plain == behind and "apport" in plain


@pytest.mark.skipif(not UBUNTU_PYTHON.exists(), reason="needs a second Python without Faultline")
def test_a_python_without_the_sdk_does_nothing_and_says_nothing() -> None:
    result = python(
        "print('ok')",
        {"PYTHONPATH": str(BOOT), "FAULTLINE_TOKEN": TOKEN},
        exe=str(UBUNTU_PYTHON),
    )
    assert (result.stdout.strip(), result.stderr) == ("ok", "")


@pytest.mark.design("boot.startup_line")
def test_with_the_token_the_sdk_says_it_is_active(control: ControlServer) -> None:
    control.open_run("run-1", "baseline", plan())
    result = python(GATES_LOADED, joined(control))
    assert result.stdout.strip() == "True True"
    assert "faultline: active for run run-1 (baseline)" in result.stderr


@pytest.mark.design("boot.unreachable_stops_program")
def test_without_its_plan_the_program_ends_before_the_agent_runs(
    control: ControlServer,
) -> None:
    for env in (
        joined(control),
        joined(control, FAULTLINE_CONTROL_URL="http://127.0.0.1:9"),
        joined(control, FAULTLINE_RUN=""),
    ):
        result = python("print('agent ran')", env)
        assert result.returncode == 1 and "agent ran" not in result.stdout
        assert "the program stops before the agent runs" in result.stderr


@pytest.mark.design("imports.refused_before_import")
def test_a_type_outside_the_allowed_prefixes_is_refused_before_import(
    control: ControlServer, tmp_path: Path
) -> None:
    marker = tmp_path / "imported"
    (tmp_path / "evil.py").write_text(
        f"open({str(marker)!r}, 'w').write('yes')\nclass Thing: pass\n"
    )
    control.open_run(
        "run-1",
        "baseline",
        plan(mocks=[{"call": "tool:a", "returns": 1, "type": "evil:Thing"}]),
    )
    result = python("print('agent ran')", joined(control), cwd=tmp_path)
    assert "evil:Thing is outside FAULTLINE_ALLOW_IMPORTS (acme.)" in result.stderr
    assert "agent ran" not in result.stdout and not marker.exists()


def test_faultline_run_needs_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_faultline_run_puts_its_folder_first() -> None:
    code = "import os, sys; print(os.environ['PYTHONPATH'].split(os.pathsep)[0])"
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAULTLINE_")}
    result = subprocess.run(
        [sys.executable, "-m", "faultline_sdk.run_command", sys.executable, "-c", code],
        env={**env, "PYTHONPATH": "/somewhere"}, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert result.stdout.strip() == str(BOOT)
