"""A Python child that would run with no gates is refused where it starts."""

import stat
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from conftest import Project, Result
from scrimmage_core import process_gate, python_args
from scrimmage_core.run_command import BOOT
from test_other_python import other_version  # noqa: F401  # a fixture
from test_scripts import RECORD, calls, script, summary_of, write_config

WRITE = "from pathlib import Path\nPath('REAL WRITE').write_text('done')\n"
STARTS = {
    "subprocess": "subprocess.run([sys.executable, 'report.py'], env=ENV)",
    "popen": "subprocess.Popen([sys.executable, 'report.py'], env=ENV).wait()",
    "exec": "os.execve(sys.executable, CHILD, ENV)",
    "spawnve": "os.waitpid(os.spawnve(os.P_NOWAIT, sys.executable, CHILD, ENV), 0)",
    "posix_spawn": "os.waitpid(os.posix_spawn(sys.executable, CHILD, ENV), 0)",
    "asyncio": "asyncio.run(run())",
}
PROGRAM = """
import asyncio, contextlib, os, subprocess, sys
ENV = {{"PATH": os.environ["PATH"]}}
CHILD = [sys.executable, "report.py"]
async def run():
    child = await asyncio.create_subprocess_exec(sys.executable, "report.py", env=ENV)
    await child.wait()
try:
    {start}
except Exception as exc:
    print(type(exc).__name__, exc, flush=True)
print("the answer")
"""


def blind_run(project: Project, start: str) -> Result:
    (project.root / "report.py").write_text(WRITE)
    return script(project, PROGRAM.format(start=start))


@pytest.mark.design("blind.refused")
@pytest.mark.parametrize("how", sorted(STARTS))
def test_a_python_child_with_its_own_environment_is_refused(project: Project, how: str) -> None:
    result = blind_run(project, STARTS[how])
    assert not (project.root / "REAL WRITE").exists()
    assert result.code == 2 and summary_of(result)["outcome"] == "refused call", result.out
    line = calls(result)[0]
    assert (line["call"], line["kind"], line["reason"]) == (
        "process:python",
        "process",
        "blind_child",
    )
    assert "Pass env={**os.environ}" in result.out and 'add "process:python"' in result.out
    jsonschema.Draft202012Validator(RECORD).validate(line)


@pytest.mark.design("blind.flags")
@pytest.mark.parametrize(
    "flags", [["-I"], ["-E"], ["-S"], ["-B", "-I"], ["-BE"], ["-W", "error", "-S"]]
)
def test_a_flag_that_skips_the_startup_file_is_refused(project: Project, flags: list[str]) -> None:
    start = f"subprocess.run([sys.executable, *{flags!r}, 'report.py'])"
    result = blind_run(project, start)
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("blind.shebang")
@pytest.mark.parametrize("line", ["{python}", "{python} -I", "/usr/bin/env {python}"])
def test_a_script_with_a_python_shebang_counts_as_python(project: Project, line: str) -> None:
    tool = project.root / "tool"
    tool.write_text(f"#!{line.format(python=sys.executable)}\n{WRITE}")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    env = "" if "-I" in line else ", env={'PATH': os.environ['PATH']}"
    result = blind_run(project, f"subprocess.run(['./tool']{env})")
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("blind.listed")
def test_listing_the_label_lets_it_run_and_leaves_a_line(project: Project) -> None:
    result = blind_run(project, STARTS["subprocess"])
    assert not (project.root / "REAL WRITE").exists()
    project.edit('real = ["tool:check_inventory"]', 'real = ["process:python"]')
    allowed = project.test()
    assert (project.root / "REAL WRITE").exists(), result.out
    assert any(
        x.get("call") == "process:python" and x.get("action") == "real"
        for lines in allowed.records()
        for x in lines
    ), allowed.out


@pytest.mark.design("blind.joined_passes")
def test_a_child_given_the_environment_with_more_in_it_runs(project: Project) -> None:
    start = "subprocess.run([sys.executable, 'report.py'], env={**os.environ, 'MORE': '1'})"
    result = blind_run(project, start)
    assert (project.root / "REAL WRITE").exists()
    assert summary_of(result)["answer"] == "the answer", result.out


@pytest.mark.design("blind.not_python_passes")
@pytest.mark.parametrize(
    "start",
    [
        "print(subprocess.run(['/bin/echo', 'x'], env={}, capture_output=True).stdout)",
        "subprocess.run(['true'], env={'PATH': os.environ['PATH']})",
        "subprocess.run('python-is-a-word', shell=True, env={'PATH': '/bin'}, stderr=-3)",
        "subprocess.run(['sh', '-c', 'true'], env={'PATH': os.environ['PATH']})",
    ],
)
def test_a_child_that_is_not_python_passes_with_a_clean_environment(
    project: Project, start: str
) -> None:
    result = script(project, PROGRAM.format(start=start))
    assert summary_of(result)["outcome"] == "answered", result.out
    assert calls(result) == []


@pytest.mark.design("blind.replaced_pythonpath")
def test_a_replaced_pythonpath_is_blind_and_the_message_says_so(project: Project) -> None:
    start = "subprocess.run(CHILD, env={**os.environ, 'PYTHONPATH': '/somewhere'})"
    result = blind_run(project, start)
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out
    assert "add to PYTHONPATH rather than replace it" in result.out


@pytest.mark.design("blind.link_to_this_binary")
def test_a_link_to_this_python_elsewhere_is_this_python(project: Project) -> None:
    (project.root / "link").mkdir()
    link = project.root / "link" / "python"
    link.symlink_to(Path(sys.executable).resolve())
    result = blind_run(project, f"subprocess.run([{str(link)!r}, 'report.py'], env=ENV)")
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("blind.base_binary")
def test_the_binary_this_venv_was_made_from_is_this_python(project: Project) -> None:
    base = str(Path(sys.base_prefix) / "bin" / Path(sys.executable).resolve().name)
    result = blind_run(project, f"subprocess.run([{base!r}, 'report.py'], env=ENV)")
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


def test_this_venvs_bin_folder_is_this_python_whatever_the_file_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    copy = tmp_path / "bin" / "python"
    copy.write_text("")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert process_gate.is_this_python(str(copy))


@pytest.mark.design("blind.cwd")
def test_a_relative_program_is_read_from_the_folder_it_starts_in(project: Project) -> None:
    folder = project.root / "deep"
    folder.mkdir()
    tool = folder / "tool"
    tool.write_text(f"#!{sys.executable}\n{WRITE}")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    result = blind_run(project, "subprocess.run(['./tool'], cwd='deep', env=ENV)")
    assert not (folder / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("blind.venv_on_this_binary_passes")
@pytest.mark.parametrize("copies", [[], ["--copies"]], ids=["links", "copies"])
def test_a_venv_elsewhere_on_this_binary_is_another_python(
    project: Project, copies: list[str]
) -> None:
    venv = project.root / "nv"
    make = [sys.executable, "-m", "venv", "--without-pip", *copies, str(venv)]
    subprocess.run(make, check=True, timeout=60, capture_output=True)
    python = venv / "bin" / "python"
    result = script(
        project, PROGRAM.format(start=f"subprocess.run([{str(python)!r}, '-c', ''], env={{}})")
    )
    assert summary_of(result)["outcome"] == "answered", result.out
    assert calls(result) == []


@pytest.mark.design("blind.other_python_passes")
def test_another_python_with_its_own_environment_runs(
    project: Project,
    other_version: Path,  # noqa: F811
) -> None:
    start = f"print(subprocess.run([{str(other_version)!r}, '-c', 'print(1)'], env={{}}))"
    result = script(project, PROGRAM.format(start=start))
    assert summary_of(result)["outcome"] == "answered", result.out
    assert calls(result) == []


@pytest.mark.design("blind.missed_through_a_shell")
def test_a_python_started_through_a_shell_is_not_seen(project: Project) -> None:
    """The listed limit: the hook sees sh, which is not Python."""
    (project.root / "report.py").write_text(WRITE)
    start = f"subprocess.run('env -i {sys.executable} report.py', shell=True)"
    result = script(project, PROGRAM.format(start=start))
    assert (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "answered", result.out


@pytest.mark.design("blind.child_refuses_too")
def test_a_child_that_joined_refuses_a_blind_grandchild(project: Project) -> None:
    (project.root / "report.py").write_text(
        "import subprocess, sys\nsubprocess.run([sys.executable, 'grandchild.py'], env={})\n"
    )
    (project.root / "grandchild.py").write_text(WRITE)
    code = "import subprocess, sys\nsubprocess.run([sys.executable, 'report.py'])\nprint('a')\n"
    result = script(project, code)
    assert not (project.root / "REAL WRITE").exists()
    assert summary_of(result)["outcome"] == "refused call", result.out


def test_a_command_it_cannot_find_is_left_to_fail_as_usual(project: Project) -> None:
    start = "subprocess.run(['no-such-program-here'])"
    result = script(project, PROGRAM.format(start=start))
    assert summary_of(result)["outcome"] == "answered", result.out
    assert calls(result) == []
    out = next((project.root / "derail-runs").glob("*/*.out")).read_text()
    assert "FileNotFoundError" in out


@pytest.mark.design("blind.mcp_default_env")
def test_a_stdio_server_on_the_librarys_small_environment_still_starts(project: Project) -> None:
    (project.root / "server.py").write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('docs')\n"
        "@server.tool(structured_output=False)\n"
        "def look(sku: str) -> str:\n"
        "    return 'real ' + sku\n"
        "server.run('stdio')\n"
    )
    (project.root / "bot.py").write_text(
        "import asyncio, sys\n"
        "from mcp import ClientSession, StdioServerParameters\n"
        "from mcp.client.stdio import stdio_client\n"
        "async def main():\n"
        "    params = StdioServerParameters(command=sys.executable, args=['server.py'])\n"
        "    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:\n"
        "        await s.initialize()\n"
        "        await s.list_tools()\n"
        "        print((await s.call_tool('look', {'sku': '1'})).content[0].text)\n"
        "asyncio.run(main())\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit('real = ["tool:check_inventory"]', 'real = ["mcp:docs/look"]')
    result = project.test()
    assert summary_of(result)["answer"] == "real 1", result.out + result.err


def test_a_shell_script_is_not_python(tmp_path: Path) -> None:
    script_file = tmp_path / "tool"
    script_file.write_text("#!/bin/sh\ntrue\n")
    assert python_args.interpreter(str(script_file), {}) is None


def test_any_derail_startup_folder_counts_and_nothing_else_does(tmp_path: Path) -> None:
    folder = tmp_path / "site-packages" / "scrimmage_core" / "_boot"
    folder.mkdir(parents=True)
    (folder / "sitecustomize.py").write_text("")
    other = str(folder)
    assert process_gate.why_blind({"DERAIL_TOKEN": "x", "PYTHONPATH": f"/a:{other}"}, []) is None
    assert process_gate.why_blind({"DERAIL_TOKEN": "x", "PYTHONPATH": "/a/_boot"}, [])
    assert process_gate.why_blind({"PYTHONPATH": other}, [])


def test_a_file_it_cannot_read_is_not_python(tmp_path: Path) -> None:
    assert python_args.interpreter(str(tmp_path / "missing"), {}) is None


def test_a_flag_after_the_program_is_the_programs_own() -> None:
    assert python_args.flags(["bot.py", "-I"]) == set()
    assert python_args.flags(["-c", "-I"]) == set()
    assert python_args.flags(["--help", "-I"]) == {"I"}
    assert python_args.flags(["-Wignore", "-S"]) == {"S"}


def test_a_lookalike_folder_is_not_derails_startup_folder(tmp_path: Path) -> None:
    fake = tmp_path / "my_scrimmage_core" / "_boot"
    empty = tmp_path / "scrimmage_core" / "_boot"
    fake.mkdir(parents=True)
    empty.mkdir(parents=True)
    (fake / "sitecustomize.py").write_text("")
    assert not process_gate.is_boot(str(fake))
    assert not process_gate.is_boot(str(empty))
    assert not process_gate.is_boot("scrimmage_core/_boot")
    assert process_gate.is_boot(str(BOOT))
