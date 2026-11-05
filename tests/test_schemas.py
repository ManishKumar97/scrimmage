"""The plan and record formats are JSON Schemas, and the design's examples fit them."""

import json
from pathlib import Path

import jsonschema
import pytest

import faultline_sdk
from conftest import EXAMPLE
from faultline_sdk import config, plan

SCHEMAS = Path(faultline_sdk.__file__).parent / "schemas"


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
    cfg = config.load(EXAMPLE / "faultline.toml")
    for row in plan.rows(cfg):
        validator("plan").validate(plan.plan_for(cfg, row, "run-1").to_json())
    assert plan.Plan.from_json(DESIGN_PLAN).to_json() == DESIGN_PLAN


def test_a_plan_with_a_renamed_tool_fits_the_schema_and_round_trips() -> None:
    doc = {**DESIGN_PLAN, "renames": {"acme.tools:get_order": "lookup-order"}}
    validator("plan").validate(doc)
    assert plan.Plan.from_json(doc).to_json() == doc
    with pytest.raises(jsonschema.ValidationError):  # only known keys, so no old spelling
        validator("plan").validate({**DESIGN_PLAN, "renamed": {}})


@pytest.mark.design("schema.record")
def test_the_designs_record_line_fits_the_schema() -> None:
    validator("record").validate(DESIGN_LINE)


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
