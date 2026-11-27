"""scrimmage init: the read that imports nothing, the probe that sends nothing, and the draft."""

import json
import os
import subprocess
import sys
import tomllib
import types
import warnings
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest

import scrimmage
from conftest import Project, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage import cli, entry
from scrimmage import init as drafting
from scrimmage import probe as probing
from scrimmage.outcomes import INFRA, MISTAKE, OK

SCHEMA = json.loads((Path(scrimmage.__file__).parent / "schemas" / "cli.v1.json").read_text())

# A ceiling only: it ends a read that would otherwise never return.
PIPE_TIMEOUT_S = 60
ENTRY = "import openai\n\n\ndef answer(question):\n    return question\n"
SCRIPT = (
    "import sys\n\n\ndef main():\n    print(sys.argv[1])\n\n\n"
    'if __name__ == "__main__":\n    main()\n'
)
# What every probed agent below shares: a client that sends to the fake server.
HEAD = "import os\nimport openai\n\nclient = openai.OpenAI(max_retries=0)\n\n\n"
CHAT = (
    'def answer(question):\n    reply = client.chat.completions.create(model="gpt-4o-mini", '
    'messages=[{"role": "user", "content": question}]TOOLS)\n    return "done"\n'
)
GET_ORDER = (
    '{"type": "function", "function": {"name": "get_order", "parameters": {"type": "object"}}}'
)


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, object]]:
    """scrimmage init --json in this process: its exit code and its one document."""
    capsys.readouterr()
    code = cli.main(["init", "--json", *args])
    captured = capsys.readouterr()
    assert captured.err == "", captured.err
    doc = cast(dict[str, object], json.loads(captured.out))
    jsonschema.validate(doc, SCHEMA)
    assert doc["exit"] == code and doc["command"] == "init"
    return code, doc


def agent(tools: str = "") -> str:
    return HEAD + CHAT.replace("TOOLS", f", tools=[{tools}]" if tools else "")


def probed(
    capsys: pytest.CaptureFixture[str],
    root: Path,
    fake: FakeOpenAI,
    monkeypatch: pytest.MonkeyPatch,
    function: str = "bot:answer",
) -> tuple[int, dict[str, object]]:
    monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    return run(capsys, str(root), "--probe", function, "--out", str(root / "draft.toml"))


def probe_of(doc: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], doc["probe"])


def messages(doc: dict[str, object], key: str) -> str:
    return " ".join(str(m["message"]) for m in cast(list[dict[str, object]], doc[key]))


def draft(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text())


def agent_table(path: Path) -> dict[str, object]:
    return cast(dict[str, object], draft(path)["agent"])


def tool_paths(doc: dict[str, object]) -> list[str]:
    return [str(t["path"]) for t in cast(list[dict[str, object]], doc["tools"])]


def names_found(doc: dict[str, object], key: str) -> list[str]:
    return [str(t["name"]) for t in cast(list[dict[str, object]], doc[key])]


# The read


@pytest.mark.design("init.never_imports")
def test_the_read_imports_nothing_from_the_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    bomb = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    write(tmp_path, {"bot.py": ENTRY, "tools_mod.py": bomb, "pkg/__init__.py": bomb})
    before = set(sys.modules)
    code, doc = run(capsys, str(tmp_path))
    assert code == OK and doc["status"] == "written"
    assert not marker.exists()
    new = set(sys.modules) - before
    assert not new & {"bot", "tools_mod", "pkg"}, new


@pytest.mark.design("init.no_probe_by_default")
def test_without_probe_no_process_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    write(tmp_path, {"bot.py": ENTRY})
    code, doc = run(capsys, str(tmp_path))
    assert code == OK and probe_of(doc) == {"ran": False, "function": None, "outcome": None}
    assert doc["numbers"] is None
    assert not (tmp_path / "scrimmage-runs").exists()
    capsys.readouterr()
    assert cli.main(["init", str(tmp_path), "--out", str(tmp_path / "b.toml")]) == OK
    out = capsys.readouterr().out
    assert "entry point (a guess): bot:answer" in out
    assert "--probe bot:answer" not in out and "--probe module:function" in out


@pytest.mark.design("init.script_not_probed")
@pytest.mark.parametrize("value", ["bot.py", "python bot.py", "bot", "a:b:c", "pkg:Class.method"])
def test_a_value_that_is_not_module_function_runs_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    write(tmp_path, {"bot.py": SCRIPT})
    code, doc = run(capsys, str(tmp_path), "--probe", value)
    assert code == MISTAKE and doc["out"] is None
    assert "a script is never probed" in messages(doc, "mistakes").lower()
    assert probe_of(doc)["ran"] is False
    assert not (tmp_path / "scrimmage-runs").exists()
    assert not (tmp_path / "scrimmage.toml").exists()


@pytest.mark.design("init.probe_needs_a_function")
def test_a_bare_probe_is_a_usage_error_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    write(tmp_path, {"bot.py": ENTRY})
    for args in (["--probe"], ["--probe", "--json"]):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["init", str(tmp_path), *args])
        assert stopped.value.code == MISTAKE
        assert "expected one argument" in capsys.readouterr().err
    assert not (tmp_path / "scrimmage.toml").exists()


@pytest.mark.design("init.script_draft")
def test_a_script_guess_is_written_and_never_probed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": SCRIPT})
    code, doc = run(capsys, str(tmp_path))
    assert code == OK and probe_of(doc)["ran"] is False
    entry = cast(dict[str, object], doc["entry"])
    assert entry["guess"] is True and "probe_command" not in entry
    assert agent_table(tmp_path / "scrimmage.toml")["command"] == "python bot.py {question}"


@pytest.mark.design("init.no_entry")
def test_no_entry_point_writes_the_commented_draft_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    write(tmp_path, {"lib.py": "def helper(a, b):\n    return a\n"})
    toml = tmp_path / "scrimmage.toml"
    code, doc = run(capsys, str(tmp_path))
    assert code == MISTAKE and doc["status"] == "mistake"
    assert doc["entry"] is None and doc["out"] == str(toml)
    assert "no entry point" in messages(doc, "mistakes")
    text = toml.read_text()
    assert '# function = "package.module:function"' in text and "\nfunction" not in text
    toml.write_text(text + '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n')
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == MISTAKE
    assert "function, command or service" in capsys.readouterr().err


@pytest.mark.design("init.ranks")
def test_candidates_are_ranked_and_the_others_are_comments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path,
        {"cli_app.py": SCRIPT, "bot.py": ENTRY, "lib.py": "def plain(query):\n    return query\n"},
    )
    _, doc = run(capsys, str(tmp_path))
    entry = cast(dict[str, object], doc["entry"])
    assert entry["kind"] == "function" and entry["value"] == "bot:answer"
    values = [c["value"] for c in cast(list[dict[str, str]], entry["candidates"])]
    assert values == ["bot:answer", "python cli_app.py {question}", "lib:plain"]
    text = (tmp_path / "scrimmage.toml").read_text()
    assert 'function    = "bot:answer"' in text
    assert "# Other entry points it saw: python cli_app.py {question}" in text
    assert "command" not in agent_table(tmp_path / "scrimmage.toml")


def test_a_function_marked_as_a_tool_is_not_an_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "import openai\n\n\ndef tool(f):\n    return f\n\n\n"
        "@tool\ndef search(query):\n    return query\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["entry"] is None


def test_a_private_function_a_method_and_extra_required_arguments_are_not_entry_points(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "import openai\n\n\ndef _answer(question):\n    return question\n\n\n"
        "def two(question, client):\n    return question\n\n\n"
        "def named(question, *, key):\n    return question\n\n\n"
        "class Bot:\n    def answer(self, question):\n        return question\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["entry"] is None


def test_an_async_entry_and_defaults_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = "import openai\n\n\nasync def answer(question, limit=3):\n    return question\n"
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert cast(dict[str, object], doc["entry"])["value"] == "bot:answer"


def test_a_test_file_loses_a_tie(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path, {"test_bot.py": ENTRY, "zbot.py": ENTRY})
    _, doc = run(capsys, str(tmp_path))
    assert cast(dict[str, object], doc["entry"])["value"] == "zbot:answer"


def test_only_five_other_entry_points_are_named_in_the_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {f"bot{n}.py": ENTRY for n in range(9)})
    _, doc = run(capsys, str(tmp_path))
    assert len(cast(dict[str, list[object]], doc["entry"])["candidates"]) == 9
    text = (tmp_path / "scrimmage.toml").read_text()
    assert text.count("# Other entry points it saw") == 5 and "# And 3 more;" in text


def test_a_broken_file_is_skipped_and_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bad.py": "def broken(:\n", "bot.py": ENTRY})
    code, doc = run(capsys, str(tmp_path))
    assert code == OK
    assert "could not read 1 files as Python: bad.py" in messages(doc, "notes")


def test_a_warning_in_the_team_s_code_stays_off_standard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY + 'PATTERN = "\\d+"\n'})
    assert run(capsys, str(tmp_path))[0] == OK  # run asserts standard error is empty


def test_the_read_records_no_warning_from_the_team_s_code(tmp_path: Path) -> None:
    write(tmp_path, {"bot.py": 'PATTERN = "\\d+"\n'})
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert drafting.parse(tmp_path / "bot.py") is not None
    assert seen == []


def test_library_and_hidden_folders_are_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for folder in (".venv", "venv", "site-packages", "node_modules", "scrimmage-runs", ".hidden"):
        write(tmp_path, {f"{folder}/x.py": ENTRY})
    code, doc = run(capsys, str(tmp_path))
    assert code == MISTAKE and doc["entry"] is None


@pytest.mark.design("init.adapter_tools")
def test_a_framework_tool_is_an_adapter_tool_with_no_tools_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "from langchain_core.tools import tool\nimport openai\n\n\n"
        "@tool\ndef get_order(order_id):\n    return order_id\n\n\n"
        "def answer(question):\n    return question\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert names_found(doc, "adapter_tools") == ["get_order"]
    assert "tools" not in agent_table(tmp_path / "scrimmage.toml") and doc["tools"] == []


@pytest.mark.design("init.methods")
def test_methods_on_a_top_level_class_and_the_draft_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "import openai\n\n\ndef is_tool(kind):\n    return lambda f: f\n\n\n"
        "class Desk:\n    @is_tool(1)\n    def lookup_ticket(self, n):\n        return n\n\n"
        "    @staticmethod\n    @is_tool(1)\n    def close_ticket(n):\n        return n\n\n"
        "    @is_tool(1)\n    async def reopen(self, n):\n        return n\n\n\n"
        "def answer(question):\n    return question\n"
    )
    write(tmp_path, {"support/desk.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert sorted(tool_paths(doc)) == [
        "support.desk:Desk.close_ticket",
        "support.desk:Desk.lookup_ticket",
        "support.desk:Desk.reopen",
    ]
    toml = tmp_path / "scrimmage.toml"
    toml.write_text(toml.read_text() + '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n')
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


@pytest.mark.design("init.decorated_methods")
def test_decorated_methods_are_found_with_no_probe_and_the_package_name_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "from tau.toolkit import is_tool\n\n\nclass AirlineTools:\n    @is_tool(1)\n"
        "    def book(self, x):\n        return x\n\n    def helper(self, x):\n        return x\n"
    )
    write(
        tmp_path,
        {"src/tau/__init__.py": "", "src/tau/air/__init__.py": "", "src/tau/air/tools.py": code},
    )
    # Read from inside the package: its import path is still the package's, not the folder's.
    inner = tmp_path / "src" / "tau" / "air"
    _, doc = run(capsys, str(inner))
    assert tool_paths(doc) == ["tau.air.tools:AirlineTools.book"]
    # Read from the checkout, a src layout: the same name, and src is not part of it.
    _, whole = run(capsys, str(tmp_path), "--out", str(tmp_path / "b.toml"))
    assert tool_paths(whole) == ["tau.air.tools:AirlineTools.book"]


@pytest.mark.design("init.unimportable_folder")
def test_a_folder_whose_name_cannot_be_imported_is_read_as_top_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "from tau.toolkit import is_tool\n\n\n@is_tool(1)\ndef lookup(x):\n    return x\n\n\n"
        "@is_tool(1)\ndef cancel(x):\n    return x\n"
    )
    folder = tmp_path / "python-backend"
    write(folder, {"__init__.py": "", "airline/__init__.py": "", "airline/tools.py": code})
    write(folder, {"my-helpers.py": "def util():\n    return 1\n"})
    _, doc = run(capsys, str(folder), "--out", str(tmp_path / "d.toml"))
    assert sorted(tool_paths(doc)) == ["airline.tools:cancel", "airline.tools:lookup"]
    notes = messages(doc, "notes")
    assert (
        "python-backend is not an importable package name, so its modules are read as "
        "top-level (airline.tools); run scrimmage from inside python-backend" in notes
    )
    # A file the read cannot name is counted, never dropped without a word.
    assert "skipped definitions in 1 files whose names cannot be imported: my-helpers.py" in notes


@pytest.mark.design("init.nested_listed")
def test_a_nested_class_method_is_listed_and_not_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "import openai\n\n\nclass Outer:\n    class Inner:\n        @is_tool(1)\n"
        "        def deep(self):\n            return 1\n\n\n"
        "def answer(question):\n    return question\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["tools"] == []
    assert doc["unresolved"] == [
        {"name": "deep", "candidates": ["bot:Outer.Inner.deep"], "problem": "nested"}
    ]
    assert (
        "Not listed, nested: deep: bot:Outer.Inner.deep"
        in (tmp_path / "scrimmage.toml").read_text()
    )


@pytest.mark.design("init.ambiguous")
def test_two_matches_are_unresolved_with_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = "@tool\ndef get_order(n):\n    return n\n"
    write(tmp_path, {"a.py": code, "b.py": code, "bot.py": ENTRY})
    _, doc = run(capsys, str(tmp_path))
    assert doc["unresolved"] == [
        {"name": "get_order", "candidates": ["a:get_order", "b:get_order"], "problem": "ambiguous"}
    ]
    assert "tools" not in agent_table(tmp_path / "scrimmage.toml")


def test_a_spec_in_a_dict_literal_finds_its_function(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        ENTRY
        + 'SPEC = {"type": "function", "function": {"name": "get_order", "parameters": {}}}\n'
        + 'OTHER = {"type": "function", "name": "get_stock", "parameters": {}}\n'
        + 'MISSING = {"name": "made_at_run_time", "parameters": {}}\n'
        + "def get_order(n):\n    return n\n\n\ndef get_stock(sku):\n    return sku\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert sorted(tool_paths(doc)) == ["bot:get_order", "bot:get_stock"]
    assert doc["unresolved"] == [
        {"name": "made_at_run_time", "candidates": [], "problem": "not_found"}
    ]


def test_a_name_the_model_sees_that_differs_is_written_with_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ENTRY + '@function_tool(name_override="lookup")\ndef get_order(n):\n    return n\n'
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["tools"] == [{"path": "bot:get_order", "name": "lookup", "found_by": "static"}]
    assert agent_table(tmp_path / "scrimmage.toml")["tools"] == [
        {"path": "bot:get_order", "name": "lookup"}
    ]


@pytest.mark.design("init.model_host")
def test_a_base_url_written_as_text_names_its_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        ENTRY
        + 'A = openai.OpenAI(base_url="https://gateway.acme.internal:8443/v1")\n'
        + 'B = openai.OpenAI(base_url="https://api.openai.com/v1")\n'
        + "C = openai.AsyncOpenAI(base_url=URL)\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["model_hosts"] == [
        {"host": "gateway.acme.internal", "provider": "openai", "found_by": "static"}
    ]
    assert agent_table(tmp_path / "scrimmage.toml")["model_hosts"] == [
        {"host": "gateway.acme.internal", "provider": "openai"}
    ]


@pytest.mark.design("init.read_hint")
def test_read_hints_are_comments_labelled_as_heuristics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        ENTRY
        + "@tool\ndef get_order(n):\n    return n\n\n\n@tool\ndef refund_order(n):\n    return n\n"
    )
    write(tmp_path, {"bot.py": code})
    run(capsys, str(tmp_path))
    text = (tmp_path / "scrimmage.toml").read_text()
    hint = next(line for line in text.splitlines() if "look like reads" in line)
    assert hint.startswith("# ") and "heuristic" in hint
    assert "get_order" in hint and "refund_order" not in hint


@pytest.mark.design("init.no_real_no_tests")
def test_the_draft_has_no_real_tool_no_test_and_no_mock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ENTRY + "@tool\ndef get_order(n):\n    return n\n"
    write(tmp_path, {"bot.py": code})
    run(capsys, str(tmp_path))
    data = draft(tmp_path / "scrimmage.toml")
    assert agent_table(tmp_path / "scrimmage.toml")["real"] == []
    assert "test" not in data and set(data) == {"agent"}


@pytest.mark.design("init.never_overwrites")
def test_init_never_replaces_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"bot.py": ENTRY})
    toml = tmp_path / "scrimmage.toml"
    toml.write_bytes(b"mine\n")
    code, doc = run(
        capsys, str(tmp_path), "--probe", "bot:answer"
    )  # no process starts: this is a fast test
    assert code == MISTAKE and doc["out"] is None and "never replaces" in messages(doc, "mistakes")
    assert toml.read_bytes() == b"mine\n"
    assert not (tmp_path / "scrimmage-runs").exists()
    toml.unlink()
    # A file that appears after the check, while the probe runs: the open itself refuses.
    real = drafting.prepare

    def appears(*args: object, **kwargs: object) -> drafting.Draft:
        made = real(*args, **kwargs)  # type: ignore[arg-type]
        toml.write_bytes(b"mine\n")
        return made

    monkeypatch.setattr(drafting, "prepare", appears)
    code, doc = run(capsys, str(tmp_path))
    assert code == MISTAKE and doc["out"] is None
    assert toml.read_bytes() == b"mine\n"


def test_a_dangling_link_counts_as_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY})
    (tmp_path / "scrimmage.toml").symlink_to(tmp_path / "gone")
    code, _ = run(capsys, str(tmp_path))
    assert code == MISTAKE and not (tmp_path / "gone").exists()


@pytest.mark.design("init.draft_checks")
def test_the_draft_plus_one_test_passes_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY + "@tool\ndef get_order(n):\n    return n\n"})
    run(capsys, str(tmp_path))
    toml = tmp_path / "scrimmage.toml"
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == MISTAKE
    assert "at least one [[test]] is required" in capsys.readouterr().err
    toml.write_text(toml.read_text() + '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n')
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


def test_a_folder_that_is_not_one_is_a_mistake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = run(capsys, str(tmp_path / "nope"))
    assert code == MISTAKE and "is not a folder" in messages(doc, "mistakes")


def test_an_out_in_a_missing_folder_is_a_mistake_and_another_folder_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"code/bot.py": ENTRY})
    missing = tmp_path / "nope" / "scrimmage.toml"
    code, doc = run(capsys, str(tmp_path / "code"), "--out", str(missing))
    assert code == MISTAKE and doc["out"] is None
    assert "its folder does not exist" in messages(doc, "mistakes")
    code, doc = run(capsys, str(tmp_path / "code"), "--out", str(tmp_path / "scrimmage.toml"))
    assert code == OK and f"move it to {(tmp_path / 'code').resolve()}" in messages(doc, "warnings")


@pytest.mark.design("init.numbers")
def test_tools_mapped_over_tools_offered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    offered = [{"name": f"tool{n}", "paths": [f"m:tool{n}"], "adapter": False} for n in range(4)]
    offered.append({"name": "two", "paths": ["a:two", "b:two"], "adapter": False})
    notice = {
        "tools": offered,
        "model_request": {
            "host": "gateway.acme.internal",
            "method": "POST",
            "path": "/v1/chat/completions",
            "provider": "openai",
        },
    }
    outcome = "stopped at POST /v1/chat/completions on gateway.acme.internal"
    monkeypatch.setattr(
        drafting,
        "run_probe",
        lambda *a: drafting.Probe(True, outcome, cast(dict[str, object], notice)),
    )
    write(tmp_path, {"bot.py": ENTRY})
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert doc["numbers"] == {"tools_offered": 5, "tools_mapped": 4}
    assert doc["unresolved"] == [
        {"name": "two", "candidates": ["a:two", "b:two"], "problem": "ambiguous"}
    ]
    assert cast(list[dict[str, str]], doc["tools"])[0]["found_by"] == "probe"
    assert doc["model_hosts"] == [
        {"host": "gateway.acme.internal", "provider": "openai", "found_by": "probe"}
    ]
    capsys.readouterr()
    assert (
        cli.main(
            ["init", str(tmp_path), "--probe", "bot:answer", "--out", str(tmp_path / "b.toml")]
        )
        == OK
    )
    assert "tools: 4 of 5 offered to the model mapped" in capsys.readouterr().out


def test_a_probe_with_no_offer_keeps_the_static_tools_and_a_name_found_only_by_the_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = {
        "tools": [],
        "model_request": {
            "host": "api.openai.com",
            "method": "POST",
            "path": "/v1/embeddings",
            "provider": "openai",
        },
    }
    monkeypatch.setattr(
        drafting,
        "run_probe",
        lambda *a: drafting.Probe(True, "stopped", cast(dict[str, object], notice)),
    )
    write(tmp_path, {"bot.py": ENTRY + "@tool\ndef get_order(n):\n    return n\n"})
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert tool_paths(doc) == ["bot:get_order"]
    assert doc["numbers"] is None and doc["model_hosts"] == []


def test_a_name_the_probe_could_not_place_is_looked_up_in_the_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = {
        "tools": [{"name": "lookup", "paths": [], "adapter": False}],
        "model_request": {
            "host": "x.test",
            "method": "POST",
            "path": "/v1/responses",
            "provider": "openai",
        },
    }
    monkeypatch.setattr(
        drafting,
        "run_probe",
        lambda *a: drafting.Probe(True, "stopped", cast(dict[str, object], notice)),
    )
    write(
        tmp_path,
        {
            "bot.py": ENTRY
            + '@tool("lookup")\ndef find(n):\n    return n\n\n\n'
            + "@tool\ndef other(n):\n    return n\n"
        },
    )
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert doc["tools"] == [{"path": "bot:find", "name": "lookup", "found_by": "probe"}]
    assert "left out of the draft: other" in messages(doc, "notes")


def test_a_failure_inside_init_is_exit_3_with_or_without_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("the read broke")

    monkeypatch.setattr(drafting, "read_folder", boom)
    write(tmp_path, {"bot.py": ENTRY})
    code, doc = run(capsys, str(tmp_path))
    assert code == INFRA and doc["status"] == "infra" and doc["out"] is None
    capsys.readouterr()
    assert cli.main(["init", str(tmp_path)]) == INFRA
    assert "the read broke" in capsys.readouterr().err


def test_an_infrastructure_failure_in_the_probe_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        drafting, "run_probe", lambda *a: drafting.Probe(True, "x", infra="it broke")
    )
    write(tmp_path, {"bot.py": ENTRY})
    code, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert code == INFRA and doc["out"] is None and not (tmp_path / "scrimmage.toml").exists()


def test_a_signal_before_the_write_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"bot.py": ENTRY})
    real = drafting.prepare

    def signalled(*args: object, **kwargs: object) -> drafting.Draft:
        entry.pending.append("SIGTERM")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drafting, "prepare", signalled)
    code, doc = run(capsys, str(tmp_path))
    assert code == INFRA and doc["out"] is None
    assert "stopped by SIGTERM" in messages(doc, "infra")
    assert not (tmp_path / "scrimmage.toml").exists()


def test_the_text_form_says_what_it_found_and_what_to_do_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ENTRY + "@tool\ndef get_order(n):\n    return n\n"
    write(tmp_path, {"bot.py": code})
    capsys.readouterr()
    assert cli.main(["init", str(tmp_path)]) == OK
    out = capsys.readouterr().out
    assert "read 1 Python files under" in out and "without importing them" in out
    assert "entry point (a guess): bot:answer (one argument, named question" in out
    assert "bot:get_order" in out and f"wrote {tmp_path / 'scrimmage.toml'}" in out
    assert out.strip().splitlines()[-1].startswith("next: write one [[test]]")


def test_a_web_service_is_noted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path, {"bot.py": "import fastapi\n" + ENTRY})
    _, doc = run(capsys, str(tmp_path))
    assert "web service" in messages(doc, "notes")


@pytest.mark.design("init.heuristics_stated")
def test_the_probe_note_says_what_it_can_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        drafting, "run_probe", lambda *a: drafting.Probe(True, drafting.NO_MODEL_REQUEST)
    )
    write(tmp_path, {"bot.py": ENTRY})
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert "would have run for real" in messages(doc, "notes")


# The probe's lookup, inside the agent's process


def loaded(
    monkeypatch: pytest.MonkeyPatch, name: str, file: Path, body: dict[str, object]
) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(file)
    vars(module).update(body)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def made_in(module: str, name: str, qualname: str | None = None) -> types.FunctionType:
    def function() -> None: ...

    function.__module__, function.__name__ = module, name
    function.__qualname__ = qualname or name
    return function


def test_paths_are_only_where_a_function_is_defined_under_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(tmp_path.resolve())
    own = made_in("team.tools", "get_order")
    loaded(monkeypatch, "team.tools", tmp_path / "team" / "tools.py", {"get_order": own})
    # Imported into another module: found only where it is defined.
    loaded(monkeypatch, "team.agent", tmp_path / "team" / "agent.py", {"get_order": own})
    other = made_in("outside", "get_order")
    loaded(monkeypatch, "outside", tmp_path.parent / "outside.py", {"get_order": other})
    library = made_in("dep", "get_order")
    loaded(
        monkeypatch, "dep", tmp_path / ".venv" / "site-packages" / "dep.py", {"get_order": library}
    )
    assert probing.paths_of("get_order", root) == ["team.tools:get_order"]


def test_methods_of_every_form_on_a_top_level_class_are_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Outer:
        class Inner:
            def deep(self) -> None: ...

    desk = type(
        "Desk",
        (),
        {
            "__module__": "team.desk",
            "plain": made_in("team.desk", "plain"),
            "still": staticmethod(made_in("team.desk", "still")),
            "cls": classmethod(made_in("team.desk", "cls")),
        },
    )
    inner = Outer.Inner
    inner.__module__ = "team.desk"
    alias = {"Desk": desk, "Other": desk, "Inner": inner}
    loaded(monkeypatch, "team.desk", tmp_path / "desk.py", alias)
    root = str(tmp_path.resolve())
    for name in ("plain", "still", "cls"):
        assert probing.paths_of(name, root) == [f"team.desk:Desk.{name}"]
    assert probing.paths_of("deep", root) == []


def test_an_empty_folder_and_a_module_that_objects_find_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Hostile(types.ModuleType):
        def __getattribute__(self, name: str) -> object:
            raise RuntimeError("do not look")

    monkeypatch.setitem(sys.modules, "hostile", Hostile("hostile"))
    monkeypatch.setitem(sys.modules, "not_a_module", cast(types.ModuleType, object()))
    assert probing.paths_of("anything", "") == []
    assert probing.paths_of("anything", str(tmp_path.resolve())) == []


# The probe


@pytest.mark.slow
@pytest.mark.design("init.probe_finds_tools")
def test_the_probe_finds_tools_sends_nothing_and_runs_no_tool(
    project: Project, fake_openai: Callable[..., FakeOpenAI], capsys: pytest.CaptureFixture[str]
) -> None:
    tools = project.root / "acme" / "tools.py"
    tools.write_text(
        tools.read_text()
        + "\n\ndef _mark(fn):\n    import functools\n    from pathlib import Path\n\n"
        + "    @functools.wraps(fn)\n    def inner(*a, **k):\n"
        + "        Path(__file__).with_name('tool-ran').touch()\n        return fn(*a, **k)\n\n"
        + "    return inner\n\n\nget_order = _mark(get_order)\n"
    )
    project.monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake = fake_openai([])
    code, doc = probed(capsys, project.root, fake, project.monkeypatch, "acme.agent:answer")
    assert code == OK, doc
    assert fake.received == []
    assert not (project.root / "acme" / "tool-ran").exists()
    assert sorted(tool_paths(doc)) == [
        "acme.tools:check_inventory",
        "acme.tools:get_order",
        "acme.tools:get_stock",
        "acme.tools:refund_order",
    ]
    assert {t["found_by"] for t in cast(list[dict[str, str]], doc["tools"])} == {"probe"}
    assert doc["numbers"] == {"tools_offered": 4, "tools_mapped": 4}
    assert "refused before it was sent" in str(probe_of(doc)["outcome"])
    assert "would have run for real" in messages(doc, "notes")
    assert doc["model_hosts"] == [{"host": "127.0.0.1", "provider": "openai", "found_by": "probe"}]
    table = agent_table(project.root / "draft.toml")
    assert table["function"] == "acme.agent:answer" and table["real"] == []
    assert len(cast(list[str], table["tools"])) == 4
    assert "test" not in draft(project.root / "draft.toml")


@pytest.mark.slow
@pytest.mark.design("init.probe_any_host")
def test_a_gateway_in_no_table_is_stopped_and_named(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"bot.py": agent(GET_ORDER)})
    fake = fake_openai([])
    code, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert code == OK and fake.received == []
    assert doc["model_hosts"] == [{"host": "127.0.0.1", "provider": "openai", "found_by": "probe"}]
    assert probe_of(doc)["ran"] is True
    assert "stopped at POST /v1/chat/completions on 127.0.0.1" in str(probe_of(doc)["outcome"])


@pytest.mark.slow
@pytest.mark.design("init.model_host")
def test_a_host_read_from_the_environment_is_found_by_the_probe(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = HEAD.replace(
        "client = openai.OpenAI(max_retries=0)",
        'client = openai.OpenAI(base_url=os.environ["GATEWAY"], max_retries=0)',
    ) + CHAT.replace("TOOLS", "")
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    monkeypatch.setenv("GATEWAY", fake.base_url)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid/v1")
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert fake.received == []
    assert doc["model_hosts"] == [{"host": "127.0.0.1", "provider": "openai", "found_by": "probe"}]


@pytest.mark.slow
@pytest.mark.design("init.probe_no_tools")
@pytest.mark.parametrize(
    "request_line",
    [
        'client.embeddings.create(model="text-embedding-3-small", input=question)',
        'client.chat.completions.create(model="gpt-4o-mini", '
        'messages=[{"role": "user", "content": question}])',
    ],
    ids=["embeddings", "chat-no-tools"],
)
def test_a_first_request_that_offers_no_tools_stops_there_and_keeps_the_candidates(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    request_line: str,
) -> None:
    code = (
        HEAD
        + f"def answer(question):\n    {request_line}\n    return 'done'\n\n\n"
        + "def is_tool(f):\n    return f\n\n\n@is_tool\ndef get_order(n):\n    return n\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    code_out, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert code_out == OK and fake.received == []
    outcome = str(probe_of(doc)["outcome"])
    assert "offers no tools" in outcome and "no tools are known" in outcome
    assert tool_paths(doc) == ["bot:get_order"]
    assert cast(list[dict[str, str]], doc["tools"])[0]["found_by"] == "static"
    assert "tools offered later are not seen" in messages(doc, "notes")
    assert doc["numbers"] is None


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_a_call_refused_before_the_model_ends_the_probe(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import httpx\n\n\ndef answer(question):\n"
        '    httpx.post("http://127.0.0.1:1/orders/refund")\n    return "done"\n'
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == []
    outcome = str(probe_of(doc)["outcome"])
    assert outcome.startswith(
        "stopped at http:127.0.0.1 POST /orders/refund, refused before it ran"
    )
    assert "no tools are known" in outcome
    assert "candidates from the code stand" in messages(doc, "notes")
    assert "would have run for real" in messages(doc, "notes")


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_an_agent_that_never_asks_the_model_ends_with_no_model_request(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"bot.py": ENTRY})
    _, doc = probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    assert probe_of(doc)["outcome"] == "saw no model request through a gated client"
    assert doc["numbers"] is None


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_a_crash_before_the_model_says_where_the_log_is(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"bot.py": ENTRY.replace("return question", "raise RuntimeError('no')")})
    _, doc = probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    outcome = str(probe_of(doc)["outcome"])
    assert outcome.startswith("crashed, see ") and outcome.endswith(".log")


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_an_agent_that_hangs_before_the_model_times_out(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    time_limit: TimeLimit,
) -> None:
    marker = tmp_path / "started"
    code = (
        "import time\nimport openai\nfrom pathlib import Path\n\n\ndef answer(question):\n"
        f"    Path({str(marker)!r}).touch()\n    time.sleep(600)\n    return question\n"
    )
    write(tmp_path, {"bot.py": code})
    time_limit.at(marker.exists)
    _, doc = probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    assert str(probe_of(doc)["outcome"]).startswith("timed out, see ")


@pytest.mark.slow
def test_an_agent_that_catches_the_refusal_runs_nothing_more(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "fallback-ran"
    code = (
        HEAD
        + "def answer(question):\n    try:\n        "
        + 'client.chat.completions.create(model="gpt-4o-mini", messages=[])\n'
        + "    except Exception:\n"
        + f"        open({str(marker)!r}, 'w').close()\n"
        + "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == []
    assert not marker.exists()
    assert "refused before it was sent" in str(probe_of(doc)["outcome"])


@pytest.mark.slow
@pytest.mark.design("init.probe_ends_after_any_refusal")
def test_an_agent_that_catches_a_refused_web_write_runs_nothing_more(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "refund-ran"
    code = (
        "import httpx\n\n\ndef refund():\n"
        f"    open({str(marker)!r}, 'w').close()\n\n\n"
        "def answer(question):\n    try:\n"
        '        httpx.post("http://127.0.0.1:1/v1/refunds", json={})\n'
        "    except Exception:\n        refund()\n    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == []
    assert not marker.exists()
    assert "refused before it ran" in str(probe_of(doc)["outcome"])


@pytest.mark.slow
@pytest.mark.design("init.probe_ends_after_any_refusal")
def test_an_agent_that_catches_a_refused_tool_runs_nothing_more(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "refund-ran"
    code = (
        "from langchain_core.tools import tool\n\n\n"
        "@tool\ndef get_order(order_id: str) -> str:\n"
        '    """Look up an order."""\n    return ""\n\n\n'
        "def refund():\n"
        f"    open({str(marker)!r}, 'w').close()\n\n\n"
        "def answer(question):\n    try:\n"
        '        get_order.invoke({"order_id": "1"})\n'
        "    except Exception:\n        refund()\n    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    rc, doc = probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    assert rc == OK
    assert not marker.exists()
    assert str(probe_of(doc)["outcome"]).startswith("stopped at tool:get_order")


@pytest.mark.slow
@pytest.mark.design("init.probe_exit_flushes")
def test_an_agent_that_ignores_sigterm_still_ends_at_once_with_its_output(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM does nothing here, so only the probe's own exit can end the agent. What it
    printed sits in a buffer, as the log is a file, and that exit must not lose it."""
    marker = tmp_path / "went-on"
    code = (
        "import signal\n"
        + HEAD
        + "def answer(question):\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + "    print('printed before the request')\n    try:\n"
        + '        client.chat.completions.create(model="gpt-4o-mini", messages=[])\n'
        + "    except Exception:\n"
        + f"        open({str(marker)!r}, 'w').close()\n"
        + "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == [] and not marker.exists()
    assert "refused before it was sent" in str(probe_of(doc)["outcome"])
    logs = list((tmp_path / "scrimmage-runs").glob("*/*.log"))
    assert "printed before the request" in logs[0].read_text()


@pytest.mark.slow
@pytest.mark.design("init.probe_exit_flushes")
def test_what_an_ordinary_agent_printed_reaches_the_log(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reaches the runner, which signals the process, so what the agent printed
    is flushed before the refusal is sent, or the signal loses it."""
    code = HEAD + (
        "def answer(question):\n    print('printed before the request')\n"
        '    client.chat.completions.create(model="gpt-4o-mini", messages=[])\n'
        "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, _ = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == []
    logs = list((tmp_path / "scrimmage-runs").glob("*/*.log"))
    assert "printed before the request" in logs[0].read_text()


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_an_entry_that_returns_none_saw_no_model_request(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"bot.py": "import openai\n\n\ndef answer(question):\n    return None\n"})
    _, doc = probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    assert probe_of(doc)["outcome"] == "saw no model request through a gated client"


REPRO = {
    "agent.py": (
        "import openai\n\n\ndef run_agent(question, session):\n    return question\n\n\n"
        "def answer(question):\n    return run_agent(question, None)\n"
    ),
    "tools.py": "import httpx\nfrom pathlib import Path\n\n\ndef delete_customer(query):\n"
    "    Path(__file__).with_name('DELETED').touch()\n",
}


@pytest.mark.design("init.probe_never_guesses")
def test_a_wrong_guess_is_never_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The real entry needs two arguments, so the read's best guess is the tool that deletes.
    agent_file = "import openai\n\n\ndef run_agent(question, session):\n    return question\n"
    write(tmp_path, {**REPRO, "agent.py": agent_file})
    with pytest.raises(SystemExit):
        cli.main(["init", str(tmp_path), "--probe"])
    assert not (tmp_path / "DELETED").exists()
    capsys.readouterr()
    code, doc = run(capsys, str(tmp_path))
    assert code == OK and not (tmp_path / "DELETED").exists()
    entry = cast(dict[str, object], doc["entry"])
    assert entry["guess"] is True and "probe_command" not in entry
    assert entry["value"] == "tools:delete_customer"  # the wrong guess this test is about
    assert "--probe tools:delete_customer" not in json.dumps(doc)
    capsys.readouterr()
    assert cli.main(["init", str(tmp_path), "--out", str(tmp_path / "b.toml")]) == OK
    out = capsys.readouterr().out
    assert "--probe tools:delete_customer" not in out
    assert "--probe module:function" in out
    text = (tmp_path / "scrimmage.toml").read_text()
    assert "# A guess from reading the code." in text


@pytest.mark.slow
@pytest.mark.design("init.probe_runs_what_was_named")
def test_the_named_function_runs_and_nothing_else(
    tmp_path: Path, fake_openai: Callable[..., FakeOpenAI], capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, REPRO)
    capsys.readouterr()
    code = cli.main(["init", str(tmp_path), "--probe", "agent:answer"])
    out = capsys.readouterr().out
    assert code == OK
    assert out.splitlines()[0] == "probe: running agent:answer up to its first model request"
    assert not (tmp_path / "DELETED").exists()
    data = agent_table(tmp_path / "scrimmage.toml")
    assert data["function"] == "agent:answer"
    assert "# A guess" not in (tmp_path / "scrimmage.toml").read_text()


def test_a_module_that_builds_a_client_is_ranked_ahead_of_the_name_tie_break(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    builds = (
        "import openai\n\nclient = openai.OpenAI()\n\n\ndef reply(message):\n    return message\n"
    )
    write(tmp_path, {"a_tools.py": ENTRY, "z_agent.py": builds})
    _, doc = run(capsys, str(tmp_path))
    entry = cast(dict[str, object], doc["entry"])
    assert entry["value"] == "z_agent:reply"
    assert "builds a model client" in str(cast(list[dict[str, str]], entry["candidates"])[0]["why"])


@pytest.mark.slow
def test_a_file_name_that_is_not_utf_8_prints_on_a_strict_stream(tmp_path: Path) -> None:
    root = bytes(tmp_path) + b"/bad\xff.py"
    with open(root, "w") as handle:
        handle.write("X = 1\n")
    write(tmp_path, {"bot.py": ENTRY})
    done = subprocess.run(
        [sys.executable, "-m", "scrimmage.cli", "init", str(tmp_path)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8:strict"},
        check=False,
    )
    assert done.returncode == OK, done.stderr
    assert b"bad\\udcff.py" in done.stdout


def test_a_very_long_elif_chain_is_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    branches = "".join(f"elif X == {n}:\n    Y = {n}\n" for n in range(1500))
    code = f"X = 1\nif X == 0:\n    Y = 0\n{branches}else:\n{'    '}Y = 1\n" + ENTRY
    write(tmp_path, {"long.py": code})
    rc, doc = run(capsys, str(tmp_path))
    assert rc == OK and cast(dict[str, object], doc["entry"])["value"] == "long:answer"


def test_a_tree_too_deep_to_walk_is_unread_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deep(*args: object) -> None:
        raise RecursionError

    monkeypatch.setattr(drafting, "read_module", deep)
    write(tmp_path, {"bot.py": ENTRY})
    code, doc = run(capsys, str(tmp_path))
    assert code == MISTAKE and "could not read 1 files" in messages(doc, "notes")


def test_a_package_that_cannot_be_imported_from_the_folder_is_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"src/pkg/__init__.py": "", "src/pkg/agent.py": ENTRY})
    _, doc = run(capsys, str(tmp_path))
    notes = messages(doc, "notes")
    assert "pkg cannot be imported from" in notes and "install it" in notes
    assert "leave out the src folder" in notes
    write(tmp_path / "flat", {"bot.py": ENTRY})
    _, plain = run(capsys, str(tmp_path / "flat"))
    assert "cannot be imported" not in messages(plain, "notes")


def test_a_file_over_the_cap_is_too_large_not_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY})
    with open(tmp_path / "big.py", "wb") as handle:
        handle.truncate(drafting.FILE_CAP + 1)
    _, doc = run(capsys, str(tmp_path))
    notes = messages(doc, "notes")
    assert "skipped 1 files over 5 MB as too large: big.py" in notes
    assert "could not read" not in notes


@pytest.mark.slow
def test_a_named_pipe_is_not_opened(tmp_path: Path) -> None:
    """In a process of its own: without the guard the read never returns, so a regression
    fails at the timeout and run() kills the child, where a hang here would leave it."""
    write(tmp_path, {"bot.py": ENTRY})
    os.mkfifo(tmp_path / "pipe.py")
    done = subprocess.run(
        [sys.executable, "-m", "scrimmage.cli", "init", "--json", str(tmp_path)],
        capture_output=True,
        timeout=PIPE_TIMEOUT_S,
        check=False,
    )
    doc = json.loads(done.stdout)
    assert done.returncode == OK and doc["entry"] is not None


def test_a_name_in_both_arms_of_an_if_is_one_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        ENTRY + "if X:\n    @tool\n    def get_order(n):\n        return n\n"
        "else:\n    @tool\n    def get_order(n):\n        return n\n"
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert tool_paths(doc) == ["bot:get_order"] and doc["unresolved"] == []


def test_text_given_to_another_decorator_is_not_the_model_s_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ENTRY + '@register_tool("Looks up an order")\ndef get_order(n):\n    return n\n'
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["tools"] == [{"path": "bot:get_order", "name": "get_order", "found_by": "static"}]


def test_a_name_with_a_newline_or_an_escape_stays_on_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ENTRY + 'SPEC = {"type": "function", "name": "a\\nb\\x1b[31m", "parameters": {}}\n'
    write(tmp_path, {"bot.py": code})
    capsys.readouterr()
    assert cli.main(["init", str(tmp_path)]) == OK
    out = capsys.readouterr().out
    assert "\x1b" not in out and "a\\nb\\x1b[31m: not mapped" in out


def test_comments_say_how_to_fix_a_nested_or_ambiguous_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = (
        "class Outer:\n    class Inner:\n        @tool\n"
        "        def deep(self):\n            return 1\n"
    )
    two = "@tool\ndef twice(n):\n    return n\n"
    write(tmp_path, {"bot.py": ENTRY + nested, "a.py": two, "b.py": two})
    run(capsys, str(tmp_path))
    text = (tmp_path / "scrimmage.toml").read_text()
    assert "move the class to the top of its module" in text
    assert 'list it as {path = "...", name = "twice"}' in text


@pytest.mark.slow
@pytest.mark.design("init.adapter_tools")
def test_the_probe_lists_a_framework_s_tools_as_adapter_tools(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "from langchain_core.tools import tool\nfrom langchain_openai import ChatOpenAI\n\n\n"
        "@tool\ndef get_order(order_id: str) -> str:\n"
        '    """Look up an order."""\n    return ""\n\n\n'
        'def answer(question):\n    model = ChatOpenAI(model="gpt-4o-mini", max_retries=0)'
        ".bind_tools([get_order])\n    model.invoke(question)\n    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert rc == OK and fake.received == []
    assert names_found(doc, "adapter_tools") == ["get_order"]
    assert doc["tools"] == []
    assert cast(list[dict[str, str]], doc["adapter_tools"])[0]["found_by"] == "probe"
    assert "tools" not in agent_table(tmp_path / "draft.toml")
    assert doc["numbers"] == {"tools_offered": 1, "tools_mapped": 0}


@pytest.mark.slow
@pytest.mark.design("init.probe_record_fields")
def test_the_probe_s_lines_fit_the_record_schema(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = "\n\ndef get_order(n):\n    return n\n"
    write(tmp_path, {"bot.py": agent(GET_ORDER) + tool})
    probed(capsys, tmp_path, fake_openai([]), monkeypatch)
    files = list((tmp_path / "scrimmage-runs").glob("*/*.jsonl"))
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    schema = json.loads(
        (Path(scrimmage.__file__).parent / "schemas" / "record.v1.json").read_text()
    )
    for line in lines:
        jsonschema.validate(line, schema)
    notice = next(line for line in lines if line.get("notice") == "probe")
    assert notice["tools"] == [{"name": "get_order", "paths": ["bot:get_order"], "adapter": False}]
    assert notice["stopped_at"] == "model:openai POST /v1/chat/completions"
    refused = next(line for line in lines if line.get("action") == "refused")
    assert refused["reason"] == "probe"


@pytest.mark.slow
@pytest.mark.design("json.init_one_document")
def test_json_is_one_document_on_a_real_process_for_every_exit_code(tmp_path: Path) -> None:
    def scrimmage(*args: str) -> tuple[int, str, str]:
        done = subprocess.run(
            [sys.executable, "-m", "scrimmage.cli", "init", "--json", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout, done.stderr

    write(tmp_path, {"good/bot.py": ENTRY, "empty/note.py": "X = 1\n"})
    good = scrimmage(str(tmp_path / "good"))
    missing = scrimmage(str(tmp_path / "empty"))
    breaks = (
        "import sys; from scrimmage import cli, init\n"
        "def boom(*a): raise RuntimeError('broke')\ninit.read_folder = boom\n"
        f"sys.exit(cli.main(['init', '--json', {str(tmp_path / 'empty')!r}, "
        f"'--out', {str(tmp_path / 'c.toml')!r}]))\n"
    )
    third = subprocess.run(
        [sys.executable, "-c", breaks], capture_output=True, text=True, check=False
    )
    for code, (returned, out, err) in zip(
        (0, 2, 3), [good, missing, (third.returncode, third.stdout, third.stderr)], strict=True
    ):
        assert returned == code and err == "", (code, err)
        doc = json.loads(out)
        jsonschema.validate(doc, SCHEMA)
        assert doc["exit"] == code and doc["status"] == cli.STATUS["init"][code]


def test_a_function_named_in_a_tool_spec_is_not_an_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = (
        "import openai\n\n\ndef search(query):\n    return query\n\n\n"
        'SPEC = {"type": "function", "name": "search", "parameters": {}}\n'
    )
    write(tmp_path, {"bot.py": code})
    _, doc = run(capsys, str(tmp_path))
    assert doc["entry"] is None and tool_paths(doc) == ["bot:search"]


def test_a_named_function_that_is_not_the_best_guess_is_the_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafting, "run_probe", lambda *a: drafting.Probe(True, "ended"))
    write(tmp_path, {"bot.py": ENTRY, "other.py": "def mine(a, b):\n    return a\n"})
    _, doc = run(capsys, str(tmp_path), "--probe", "other:mine")
    entry = cast(dict[str, object], doc["entry"])
    assert entry["value"] == "other:mine" and entry["guess"] is False
    assert agent_table(tmp_path / "scrimmage.toml")["function"] == "other:mine"
    assert probe_of(doc)["function"] == "other:mine"


def test_an_unreadable_file_name_is_stored_as_text(tmp_path: Path) -> None:
    with open(bytes(tmp_path) + b"/bad\xff.py", "w") as handle:
        handle.write("X = 1\n")
    assert drafting.read_folder(tmp_path).unread == ["bad\\udcff.py"]


@pytest.mark.slow
def test_a_caught_refusal_in_a_normal_run_lets_the_code_after_it_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe ends a program at its first refusal. A scrimmage test must not: the agent's
    own handler runs, as it would in production."""
    marker = tmp_path / "fallback-ran"
    code = (
        "import httpx\n\n\ndef answer(question):\n    try:\n"
        '        httpx.post("http://127.0.0.1:1/v1/refunds", json={})\n'
        "    except Exception:\n"
        f"        open({str(marker)!r}, 'w').close()\n"
        "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    (tmp_path / "scrimmage.toml").write_text(
        '[agent]\nfunction = "bot:answer"\nruns = 1\nmax_cost_usd = "none"\n\n'
        '[[test]]\nname = "t"\nquestion = "go"\nexpect = ["done"]\n'
    )
    capsys.readouterr()
    cli.main(["test", "--json", str(tmp_path / "scrimmage.toml")])
    out = json.loads(capsys.readouterr().out)
    assert marker.exists()
    assert "refused call" in json.dumps(out)


def test_a_probe_of_a_module_that_cannot_be_imported_is_a_mistake_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: object) -> None:
        raise AssertionError("nothing may run for a module that is not there")

    monkeypatch.setattr(drafting, "run_probe", never)
    write(tmp_path, {"bot.py": ENTRY})
    out = tmp_path / "draft.toml"
    rc, doc = run(capsys, str(tmp_path), "--probe", "nope_nothing:nope", "--out", str(out))
    assert rc == MISTAKE
    assert "nope_nothing cannot be imported" in messages(doc, "mistakes")
    assert "install it" in messages(doc, "mistakes")
    assert probe_of(doc)["ran"] is False
    assert not out.exists()


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_a_probe_of_a_missing_submodule_is_a_mistake_and_writes_nothing(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"evil/__init__.py": "", "evil/real.py": ENTRY})
    fake = fake_openai([])
    rc, doc = probed(capsys, tmp_path, fake, monkeypatch, "evil.missing:answer")
    assert rc == MISTAKE and fake.received == []
    assert "evil.missing cannot be imported" in messages(doc, "mistakes")
    assert not (tmp_path / "draft.toml").exists()


@pytest.mark.slow
@pytest.mark.design("init.probe_outcome")
def test_a_module_the_function_cannot_import_is_still_a_crash_not_a_bad_name(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(tmp_path, {"bot.py": "import nothing_installed_here\n\n\ndef answer(q):\n    return q\n"})
    fake = fake_openai([])
    _, doc = probed(capsys, tmp_path, fake, monkeypatch)
    assert "cannot be imported" not in messages(doc, "mistakes")
    assert str(probe_of(doc)["outcome"]).startswith("crashed, see ")


def test_a_probe_of_a_module_in_the_folder_is_still_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(
        drafting, "run_probe", lambda function, *a: ran.append(function) or drafting.Probe(True)
    )
    write(tmp_path, {"bot.py": ENTRY})
    run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert ran == ["bot:answer"]


def test_a_tree_over_the_file_limit_is_cut_with_a_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafting, "FILE_LIMIT", 3)
    write(tmp_path, {f"m{n}.py": "X = 1\n" for n in range(5)})
    _, doc = run(capsys, str(tmp_path))
    assert "read the first 3 .py files and stopped; skipped 2 .py files" in messages(doc, "notes")
    reading = drafting.read_folder(tmp_path)
    assert reading.files == 3  # the note's numbers are the count read and the count left
    assert reading.skipped == 2
    write(tmp_path / "small", {f"m{n}.py": "X = 1\n" for n in range(3)})
    _, exact = run(capsys, str(tmp_path / "small"))
    assert "stopped" not in messages(exact, "notes")


def test_a_timed_out_probe_is_not_a_missing_module_whatever_its_log_says(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        'Traceback (most recent call last):\n  File "x.py", line 1, in f\n'
        "ModuleNotFoundError: No module named 'evil.missing'\n"
    )
    for outcome, expected in (("crashed", "evil.missing"), ("timed out", None)):
        result = cast(Any, SimpleNamespace(notices=[], outcome=outcome))
        probe = drafting.read_probe(result, [], log, "evil.missing:answer")
        assert probe.missing == expected, outcome


def test_the_skipped_count_stops_at_its_bound_and_asks_nothing_of_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafting, "FILE_LIMIT", 2)
    monkeypatch.setattr(drafting, "SKIP_COUNT_LIMIT", 4)
    write(tmp_path, {f"m{n}.py": "X = 1\n" for n in range(12)})
    seen: list[str] = []
    real = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: seen.append(self.name) or real(self))
    _, doc = run(capsys, str(tmp_path))
    assert "skipped at least 5 .py files" in messages(doc, "notes")
    asked = [name for name in seen if name.startswith("m")]
    assert asked == ["m0.py", "m1.py", "m10.py"]  # two read and one past the cap, then no more


def test_a_log_ending_on_another_error_is_not_a_missing_module(tmp_path: Path) -> None:
    frames = 'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    import y\n'
    log = tmp_path / "run.log"
    log.write_text(frames + "ModuleNotFoundError: No module named 'evil.missing'\n")
    assert drafting.missing_module(log, "evil.missing:answer") == "evil.missing"
    assert drafting.missing_module(log, "evil.real:answer") is None
    # a worker logged a missing module, then the program died of something else
    log.write_text(
        frames
        + "ModuleNotFoundError: No module named 'evil.missing'\n\n"
        + frames
        + "RuntimeError: boom\n"
    )
    assert drafting.missing_module(log, "evil.missing:answer") is None


def test_a_py_symlink_that_leaves_the_folder_is_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path / "elsewhere", {"secret.py": ENTRY})
    write(tmp_path / "code", {"inside.py": "X = 1\n"})
    (tmp_path / "code" / "out.py").symlink_to(tmp_path / "elsewhere" / "secret.py")
    (tmp_path / "code" / "in.py").symlink_to(tmp_path / "code" / "inside.py")
    reading = drafting.read_folder((tmp_path / "code").resolve())
    assert reading.outside == ["out.py"] and reading.files == 2
    _, doc = run(capsys, str(tmp_path / "code"))
    notes = messages(doc, "notes")
    assert "skipped 1 .py symlinks that point outside the folder: out.py" in notes
    assert doc["entry"] is None
