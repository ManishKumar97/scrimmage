"""The formats are JSON Schemas: the plan, the record and scrimmage.toml, held to the code."""

import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import cast

import jsonschema
import pytest

import scrimmage_core
from config_cases import AGENT, BAD, GOOD, MINIMAL, TEST, Bad
from conftest import EXAMPLE
from scrimmage_core import cli, config, plan
from scrimmage_core.errors import ConfigError

SCHEMAS = Path(scrimmage_core.__file__).parent / "schemas"
TESTS = Path(__file__).parent
DOCS = TESTS.parent / "docs"


def schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMAS / f"{name}.v1.json").read_text())


def validator(name: str) -> jsonschema.Draft202012Validator:
    body = schema(name)
    jsonschema.Draft202012Validator.check_schema(body)
    return jsonschema.Draft202012Validator(body, format_checker=jsonschema.FormatChecker())


# The design's example plan and record line, trimmed to the fields this version has.
DESIGN_PLAN = {
    "v": 1,
    "run": "run-7f3a9c",
    "test": "order-status",
    "row": "fault 1",
    "mocks": [{"call": "tool:get_order", "returns": "shipped, Oct 30"}],
    "faults": [{"call": "tool:get_order", "do": "timeout", "when": "always"}],
    "tools": ["acme.tools:get_order", "acme.tools:check_inventory"],
    "real": ["tool:check_inventory"],
    "children": False,
    "time_limit_s": 120,
    "model_hosts": [],
    "budget": {"limit_usd": 5.0, "reserve_output_tokens": 4096, "prices": {}},
}
DESIGN_LINE = {
    "v": 1,
    "run": "run-17",
    "test": "order-status",
    "row": "fault 1",
    "seq": 2,
    "proc": {"host": "acme-agent-7d9f", "pid": 4121, "started": "2026-11-04T10:00:01Z"},
    "call": "tool:get_order",
    "kind": "tool",
    "action": "fault",
    "fault": {"do": "timeout", "when": "always", "n": 1},
    "args": {
        "sha256": "9f2c41" + "0" * 58,
        "bytes": 19,
        "preview": '{"order_id": "1234"}',
    },
    "result": None,
    "error": "TimeoutError: get_order timed out",
    "start": "2026-11-04T10:00:03.120Z",
    "ms": 2,
}


@pytest.mark.design("schema.plan")
def test_the_designs_plan_fits_the_schema(tmp_path: Path) -> None:
    validator("plan").validate(DESIGN_PLAN)
    cfg = config.load(EXAMPLE / "scrimmage.toml")
    for row in plan.rows(cfg):
        validator("plan").validate(plan.plan_for(cfg, row, "run-1").to_json())
    assert plan.Plan.from_json(DESIGN_PLAN).to_json() == DESIGN_PLAN


def test_a_plan_with_a_renamed_tool_fits_the_schema_and_round_trips() -> None:
    doc = {**DESIGN_PLAN, "renames": {"acme.tools:get_order": "lookup-order"}}
    validator("plan").validate(doc)
    assert plan.Plan.from_json(doc).to_json() == doc
    with pytest.raises(jsonschema.ValidationError):  # only known keys, so no old spelling
        validator("plan").validate({**DESIGN_PLAN, "renamed": {}})


def test_a_served_plan_with_the_runs_age_fits_the_schema_and_round_trips() -> None:
    doc = {**DESIGN_PLAN, "elapsed_s": 12.5}
    validator("plan").validate(doc)
    assert plan.Plan.from_json(doc).to_json() == doc
    with pytest.raises(jsonschema.ValidationError):
        validator("plan").validate({**DESIGN_PLAN, "elapsed_s": -1})


@pytest.mark.design("schema.record")
def test_the_designs_record_line_fits_the_schema() -> None:
    validator("record").validate(DESIGN_LINE)
    miss = {
        "reason": "arguments",
        "seen": '{"sku": "SKU-42"}',
        "method": None,
        "path": None,
        "nearest": {
            "mock": 3,
            "args": '{"sku": "42"}',
            "method": None,
            "path": None,
            "differs": ["sku"],
            "differs_left_out": 0,
        },
    }
    validator("record").validate({**DESIGN_LINE, "action": "real", "mock_miss": miss})
    validator("record").validate({**DESIGN_LINE, "args_not_plain": True})
    with pytest.raises(jsonschema.ValidationError):
        validator("record").validate({**DESIGN_LINE, "near_mock": True})


def test_a_line_with_an_unknown_field_does_not_fit() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validator("record").validate({**DESIGN_LINE, "extra": 1})
    with pytest.raises(jsonschema.ValidationError):
        validator("plan").validate({**DESIGN_PLAN, "faults": [{"call": "model", "do": "x"}]})


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {**DESIGN_PLAN, "v": 2},
        {**DESIGN_PLAN, "mocks": "x"},
        {**DESIGN_PLAN, "run": 1},
        {**DESIGN_PLAN, "time_limit_s": "soon"},
        {**DESIGN_PLAN, "mocks": [{"call": "tool:a", "returns": 1, "type": 3}]},
    ],
)
def test_the_sdk_rejects_a_bad_plan(bad: object) -> None:
    with pytest.raises(ValueError):
        plan.Plan.from_json(bad)


@pytest.mark.design("schema.plan_model_name")
def test_a_plan_with_a_named_model_fits_the_schema() -> None:
    fault = {"call": "model", "do": "status 429", "when": "first 3", "model": "gpt-4.1"}
    assert list(validator("plan").iter_errors({**DESIGN_PLAN, "faults": [fault]})) == []
    empty = {**fault, "model": ""}
    assert list(validator("plan").iter_errors({**DESIGN_PLAN, "faults": [empty]}))


@pytest.mark.design("schema.unreadable_model")
def test_a_refusal_for_a_name_that_cannot_be_read_fits_the_schema() -> None:
    line = {**DESIGN_LINE, "action": "refused", "reason": "unreadable_model"}
    line.pop("fault")
    assert list(validator("record").iter_errors(line)) == []


CONFIG_TABLES = {
    "agent": config.AGENT_KEYS,
    "test": config.TEST_KEYS,
    "tool_mock": config.MOCK_KEYS,
    "web_mock": config.WEB_MOCK_KEYS,
    "mcp_mock": config.MCP_MOCK_KEYS,
    "tool_fault": config.FAULT_KEYS,
    "model_fault": config.MODEL_FAULT_KEYS,
    "web_fault": config.WEB_FAULT_KEYS,
    "mcp_fault": config.MCP_FAULT_KEYS,
    "price": config.PRICE_KEYS,
}


def toml_blocks() -> list[tuple[str, str]]:
    """Every TOML block in the docs, by page."""
    blocks: list[tuple[str, str]] = []
    for page in sorted(DOCS.glob("*.md")):
        found = re.findall(r"```toml\n(.*?)```", page.read_text(), flags=re.S)
        blocks += [(f"{page.name} {i}", block) for i, block in enumerate(found, 1)]
    return blocks


@pytest.mark.design("schema.config_prints")
def test_scrimmage_schema_prints_the_packaged_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["schema"]) == 0
    out = capsys.readouterr().out
    assert out == (SCHEMAS / "scrimmage-toml.v1.json").read_text()
    jsonschema.Draft202012Validator.check_schema(json.loads(out))


@pytest.mark.design("schema.config_keys")
def test_the_schemas_keys_are_the_parsers_keys() -> None:
    top = schema("scrimmage-toml")
    defs = cast(dict[str, dict[str, object]], top["$defs"])
    for name, keys in CONFIG_TABLES.items():
        table = defs[name]
        assert set(cast(dict[str, object], table["properties"])) == keys, name
        assert table["additionalProperties"] is False, name
    assert set(cast(dict[str, object], top["properties"])) == {"agent", "test"}
    assert top["additionalProperties"] is False


@pytest.mark.design("schema.config_accepts")
def test_every_file_the_parser_accepts_fits_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A docs block's command names python or python3; find them as a venv user would.
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    files = [
        (str(path.relative_to(TESTS)), path.read_text())
        for path in sorted(TESTS.rglob("*.toml"))
        if "fixtures" not in path.parts
    ]
    # A docs block is often part of a file: a fault, an [agent] table or a few of its keys.
    # Each is also tried inside a minimal file, so the part is checked in a whole one.
    blocks = [
        (f"{name}: {how}", text)
        for name, block in toml_blocks()
        for how, text in (
            ("alone", block),
            ("with a test", block + TEST),
            ("after a test", f"{MINIMAL}\n{block}"),
            ("in [agent]", block.replace("[agent]\n", AGENT, 1) + TEST),
            ("under [agent]", AGENT + block + TEST),
        )
    ]
    accepted: list[str] = []
    for name, text in [*files, *GOOD.items(), *blocks]:
        path = tmp_path / "scrimmage.toml"
        path.write_text(text)
        try:
            config.load(path)
        except ConfigError:
            continue
        errors = [e.message for e in validator("scrimmage-toml").iter_errors(tomllib.loads(text))]
        assert not errors, (name, errors)
        accepted.append(name)
    assert {"example/scrimmage.toml", *GOOD} <= set(accepted)
    checked = {name.partition(":")[0] for name in accepted if ".md " in name}
    # Only model-faults.md 3 is left: two keys of a fault, shown on their own.
    assert len(checked) == len(toml_blocks()) - 1, sorted(checked)


@pytest.mark.design("schema.config_rejects")
@pytest.mark.parametrize("bad", BAD, ids=[bad.name for bad in BAD])
def test_a_bad_file_is_rejected_by_the_parser_and_a_bad_shape_by_the_schema(
    tmp_path: Path, bad: Bad
) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text(bad.text)
    with pytest.raises(ConfigError):
        config.load(path)
    if bad.shape:
        assert list(validator("scrimmage-toml").iter_errors(tomllib.loads(bad.text))), bad.name


def test_there_are_at_least_thirty_bad_shapes() -> None:
    assert sum(bad.shape for bad in BAD) >= 30
