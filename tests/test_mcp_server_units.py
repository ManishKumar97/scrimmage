"""The Scrimmage MCP server, in this process: its schemas, its arguments and its paths.

Nothing here starts a process; tests/test_mcp_server.py drives the server over stdio.
"""

import json
import re
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from conftest import Project
from report_cases import CASES
from scrimmage_core import cli, mcp_server, show
from scrimmage_core.runner import Runner
from show_cases import case_result, write_folder

CLI_SCHEMA = mcp_server.cli_schema()


def settings(root: Path, **changes: Any) -> mcp_server.Settings:
    values: dict[str, Any] = {
        "root": root,
        "config": root / "scrimmage.toml",
        "max_session_usd": 10.0,
        "allow_no_budget": False,
        "allow_probe": False,
        "call_timeout": 1800.0,
        **changes,
    }
    return mcp_server.Settings(**values)


def documents(project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A document of each command at each exit code it can give in this process, as the
    --json tests make them, and two that fit no schema."""

    def printed(*argv: str) -> dict[str, Any]:
        project.capsys.readouterr()
        cli.main([*argv, "--json"])
        return cast(dict[str, Any], json.loads(project.capsys.readouterr().out))

    toml = str(project.toml)
    docs: list[Any] = [case_result(name)[0] for name in sorted(CASES)]
    docs += [printed("check", toml), printed("check", toml, "--runs", "0")]
    docs.append(printed("record", toml, "--test", "nobody"))
    docs.append(printed("init", str(tmp_path / "nowhere")))
    docs.append(printed("init", str(project.root), "--out", str(tmp_path / "draft.toml")))
    folder, _, _ = write_folder(tmp_path)
    docs += [printed("show", str(folder)), printed("show", str(tmp_path / "nowhere"))]
    with monkeypatch.context() as patch:
        patch.setattr(Runner, "one", lambda self, row: None)
        docs.append(printed("test", toml))

        def broken(*_: object) -> None:
            raise RuntimeError("show broke")

        patch.setattr(show, "build", broken)
        docs.append(printed("show", str(folder)))
    docs.append({**docs[0], "exit": "one"})  # fits no schema
    docs.append({**docs[0], "rows": "none"})
    return docs


def fits(schema: dict[str, object], doc: Any) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(doc)


def test_each_output_schema_is_built_from_the_shipped_cli_schema(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = cast(dict[str, object], CLI_SCHEMA["$defs"])
    for command, schema in mcp_server.OUTPUT_SCHEMAS.items():
        jsonschema.Draft202012Validator.check_schema(schema)
        defs = cast(dict[str, object], schema["$defs"])
        assert {"envelope", command} <= set(defs), command
        for name, definition in defs.items():
            assert definition == shipped[name], (command, name)
    docs = documents(project, tmp_path, monkeypatch)
    commands = {doc["command"] for doc in docs}
    assert commands == set(mcp_server.COMMANDS)
    for doc in docs:
        command = doc["command"]
        assert fits(mcp_server.OUTPUT_SCHEMAS[command], doc) == fits(CLI_SCHEMA, doc), doc
    assert sum(not fits(CLI_SCHEMA, doc) for doc in docs) == 2  # both ways reject the same two


def test_each_output_schema_holds_every_definition_it_refers_to() -> None:
    for command, schema in mcp_server.OUTPUT_SCHEMAS.items():
        assert schema["type"] == "object", command  # protocol versions up to 2025-11-25
        defs = cast(dict[str, object], schema["$defs"])
        assert mcp_server.references(schema) <= set(defs), command
        # Every reference resolves inside the document: no outside registry is needed.
        jsonschema.Draft202012Validator(schema).is_valid({})
    sizes = {
        c: len(json.dumps(s, separators=(",", ":"))) for c, s in mcp_server.OUTPUT_SCHEMAS.items()
    }
    assert sizes["check"] < sizes["show"], sizes  # each holds only what it needs


WRONG_TYPES = {
    "test": [{"max_cost_usd": "1"}, {"max_cost_usd": 0}, {"max_cost_usd": 1001},
             {"max_cost_usd": 1, "compare": 3}, {"max_cost_usd": 1, "compare": ""}],
    "record": [{"max_cost_usd": 1, "runs": 0}, {"max_cost_usd": 1, "runs": 1.5},
               {"max_cost_usd": 1, "tests": "t"}, {"max_cost_usd": 1, "tests": ["a", "a"]},
               {"max_cost_usd": 1, "tests": ["-x"]}, {"max_cost_usd": 1, "refresh": ["a\nb"]},
               {"max_cost_usd": 1, "tests": ["t"] * 101}],
    "init": [{"folder": 1}, {"folder": "x" * 1025}],
    "show": [{"run": "run-1"}, {"target": None}, {"compare": []}],
    "check": [],
}  # fmt: skip


def test_each_input_schema_refuses_an_unknown_key_and_a_wrong_type(tmp_path: Path) -> None:
    state = mcp_server.Scrimmage(settings(tmp_path))
    for name in mcp_server.COMMANDS:
        base = {"max_cost_usd": 1} if name in ("test", "record") else {}
        assert state.checked(name, dict(base)) == base, name
        with pytest.raises(mcp_server.Refused, match="bogus"):
            state.checked(name, {**base, "bogus": 1})
        for arguments in WRONG_TYPES[name]:
            with pytest.raises(mcp_server.Refused):
                state.checked(name, arguments)
    with pytest.raises(mcp_server.Refused, match="no tool is named 'schema'"):
        state.checked("schema", {})


def test_a_run_id_or_probe_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    state = mcp_server.Scrimmage(settings(tmp_path, allow_probe=True))
    assert state.checked("show", {"run": "run-0123456789ab"})
    assert state.checked("init", {"probe": "acme.agent:answer"})
    validator = state.validators["show"]
    assert validator.is_valid({"run": "run-0123456789ab\n"})  # the schema alone lets it pass
    with pytest.raises(mcp_server.Refused, match="run: 'run-0123456789ab\\\\n'"):
        state.checked("show", {"run": "run-0123456789ab\n"})
    with pytest.raises(mcp_server.Refused, match="probe:"):
        state.checked("init", {"probe": "acme.agent:answer\n"})


OUTSIDE = {
    "dotdot": "../x",
    "absolute_outside": "/etc",
    "leading_dash": "-x",
    "leading_tilde": "~/x",
    "nul": "a\x00b",
    "newline": "a\nb",
    "escape": "a\x1b[2Jb",
    "too_long": "a/" * 512 + "b",
    "part_too_long": "é" * 128,  # 256 bytes in UTF-8, in 128 characters
}


@pytest.mark.parametrize("value", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_a_path_outside_the_root_is_refused(tmp_path: Path, value: str) -> None:
    root = tmp_path / "app"
    root.mkdir()
    with pytest.raises(mcp_server.Refused):
        mcp_server.inside(value, root)
    assert mcp_server.inside("x", root) == root / "x"  # the same rule lets a plain name in
    assert mcp_server.inside("é" * 127, root) == root / ("é" * 127)


def test_a_sibling_folder_with_the_roots_name_as_prefix_is_outside(tmp_path: Path) -> None:
    root, sibling = tmp_path / "app", tmp_path / "app2"
    root.mkdir()
    sibling.mkdir()
    for value in (str(sibling / "x"), "../app2/x", str(sibling)):
        with pytest.raises(mcp_server.Refused, match="outside the project folder"):
            mcp_server.inside(value, root)
    assert mcp_server.inside(str(root / "x"), root) == root / "x"
    assert mcp_server.inside(str(root), root) == root
    assert mcp_server.inside("sub/../x", root) == root / "x"


def test_a_link_that_points_out_is_refused(tmp_path: Path) -> None:
    root, outside = tmp_path / "app", tmp_path / "elsewhere"
    (root / "runs").mkdir(parents=True)
    outside.mkdir()
    (root / "out").symlink_to(outside)
    (root / "hop").symlink_to(root / "hop2")
    (root / "hop2").symlink_to(root / "out")  # a chain of links that ends outside
    (root / "in").symlink_to(root / "runs")
    for value in ("out", "out/x", "hop", "hop/x", str(root / "out")):
        with pytest.raises(mcp_server.Refused, match="outside the project folder"):
            mcp_server.inside(value, root)
    assert mcp_server.inside("in", root) == root / "runs"  # the child gets the target
    assert mcp_server.inside("in/x", root) == root / "runs" / "x"


def every_argv(state: mcp_server.Scrimmage, value: str) -> list[list[str]]:
    usd = {"max_cost_usd": 0.5}
    return [
        state.argv("test", {**usd, "compare": value}),
        state.argv("record", {**usd, "tests": [value], "refresh": [value], "runs": 2}),
        state.argv("init", {"folder": value, "probe": "acme.agent:answer"}),
        state.argv("show", {"target": value, "run": "run-0123456789ab", "compare": value}),
        state.argv("check", {}),
    ]


def test_a_value_never_becomes_a_flag(tmp_path: Path) -> None:
    state = mcp_server.Scrimmage(settings(tmp_path, allow_probe=True))
    for argv in every_argv(state, "x"):
        for arg in argv:
            assert arg.startswith("/") or re.fullmatch(r"--[a-z-]+=.+", arg, re.S), (argv, arg)
    assert every_argv(state, "x")[0][1] == "--max-cost-usd=0.5"
    for value in ("-x", "--compare", "-"):
        with pytest.raises(mcp_server.Refused, match="may not start with -"):
            every_argv(state, value)
        for name, arguments in (("record", {"max_cost_usd": 1, "tests": [value]}),):
            with pytest.raises(mcp_server.Refused, match=r"tests\.0"):
                state.checked(name, arguments)


def test_a_relative_value_is_read_against_the_root(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    state = mcp_server.Scrimmage(settings(tmp_path, config=tmp_path / "sub" / "scrimmage.toml"))
    folder = "sub/scrimmage-runs/20261111T101500Z-a1b2c3"
    assert state.argv("show", {"target": folder})[0] == str(tmp_path / folder)
    assert state.argv("show", {"target": "./" + folder})[0] == str(tmp_path / folder)
    test = state.argv("test", {"max_cost_usd": 1, "compare": folder})
    assert test == [str(tmp_path / "sub" / "scrimmage.toml"), "--max-cost-usd=1.0",
                    f"--compare={tmp_path / folder}"]  # fmt: skip
