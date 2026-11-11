"""The Scrimmage MCP server, in this process: its schemas, its arguments and its paths.

Nothing here starts a process; tests/test_mcp_server.py drives the server over stdio.
"""

import json
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
