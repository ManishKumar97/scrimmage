"""The formats are JSON Schemas: the plan, the record and scrimmage.toml, held to the code."""

import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

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


@pytest.mark.design("schema.plan")
def test_a_plan_that_lists_a_program_fits_the_schema(tmp_path: Path) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text(
        '[agent]\nservice = "http://127.0.0.1:9/chat"\nbody = {message = "{question}"}\n'
        'answer = "reply"\nreal = ["process:uname", "process:python", "process:g++"]\n'
        'background_real = ["process:uname"]\n\n[[test]]\nname = "t"\nquestion = "q"\n'
        'expect = ["x"]\n'
    )
    cfg = config.load(path)
    doc: Any = cast(
        dict[str, Any], plan.plan_for(cfg, plan.rows(cfg)[0], "run-0123456789ab").to_json()
    )
    assert doc["real"] == ["process:uname", "process:python", "process:g++"]
    validator("plan").validate(json.loads(json.dumps(doc)))
    for bad in ["process:", "process:a\n", "process:a b", "process:?"]:
        assert list(validator("plan").iter_errors({**doc, "real": [bad]})), bad


@pytest.mark.design("schema.record")
def test_a_process_call_label_fits_the_record_schema_only_as_a_plain_name() -> None:
    for name in ["process:uname", "process:python3.12", "process:g++", "process:?"]:
        assert list(validator("record").iter_errors({**DESIGN_LINE, "call": name})) == [], name
    for name in ["process:", "process:a\n", "process:a\nb", "process: ", "process:??"]:
        assert list(validator("record").iter_errors({**DESIGN_LINE, "call": name})), name


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
    # A docs block for a service reads its bearer token from the environment, as a team's does.
    monkeypatch.setenv("TEST_TOKEN", "tt-0123456789abcdef")
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
    # Only model-faults.md 4 is left: two keys of a fault, shown on their own.
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


@pytest.mark.design("schema.startup_report_additive")
def test_a_later_field_in_the_control_objects_fits_both_schemas() -> None:
    """Neither schema closes the object, so a field added later does not fail this copy."""
    startup = json.loads((SCHEMAS / "startup-report.v2.json").read_text())["$defs"]["control"]
    cli_doc = json.loads((SCHEMAS / "cli.v1.json").read_text())
    ping = cli_doc["$defs"]["test"]["properties"]["control_ping"]
    found = {
        "reached": True,
        "tls": "none",
        "error": None,
        "detail": None,
        "rtt_ms": 1.0,
        "runner_id": None,
        "url": "http://127.0.0.1:1",
        "from_a_later_release": 1,
    }
    assert startup.get("additionalProperties") is not False
    assert ping.get("additionalProperties") is not False
    for control in (startup, ping):
        assert not list(jsonschema.Draft202012Validator(control).iter_errors(found))


Func = ast.FunctionDef | ast.AsyncFunctionDef


class Gates:
    """The gate modules of one package, read as syntax, to find every refusal reason written.

    A reason is read where it is written: a `reason=` keyword on any call, the second argument
    of `.refuse(step, reason)`, `x["reason"] = ...` and `x.reason = ...`. Its value may be a
    string, a module constant here or in another gate module (`engine.MISTAKE_REFUSAL`), a local
    name assigned from those, or an if-else or `or` of them.
    A read of another `.reason` passes on a reason written (and read) elsewhere. Anything else
    is listed in `unreadable`, so a new way of writing a reason fails the test instead of
    slipping past it."""

    def __init__(self, package: Path) -> None:
        # runner.py reads reasons from the records and writes none
        paths = [p for p in sorted(package.glob("*.py")) if p.name != "runner.py"]
        self.trees = {p.stem: ast.parse(p.read_text()) for p in paths}
        self.files = {stem: package / f"{stem}.py" for stem in self.trees}
        self.found: set[str] = set()
        self.unreadable: list[str] = []
        for stem, tree in self.trees.items():
            self.walk(stem, tree, None)

    def walk(self, stem: str, node: ast.AST, func: Func | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            func = node
        for value, line in self.written(node):
            self.take(stem, value, func, line)
        for child in ast.iter_child_nodes(node):
            self.walk(stem, child, func)

    def written(self, node: ast.AST) -> list[tuple[ast.expr, int]]:
        """The expressions this one node writes as a refusal reason."""
        out: list[tuple[ast.expr, int]] = []
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).rpartition(".")[2]
            out += [(k.value, node.lineno) for k in node.keywords if k.arg == "reason"]
            if name == "Decision" and len(node.args) > 1:
                out.append((ast.Name(id="<positional Decision argument>"), node.lineno))
            if name == "refuse" and isinstance(node.func, ast.Attribute):
                if len(node.args) > 1:
                    out.append((node.args[1], node.lineno))
                elif not any(k.arg == "reason" for k in node.keywords):
                    out.append((ast.Name(id="<refuse without a reason>"), node.lineno))
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                keyed = isinstance(t, ast.Subscript) and ast.unparse(t.slice) in (
                    "'reason'",
                    '"reason"',
                )
                if keyed or (isinstance(t, ast.Attribute) and t.attr == "reason"):
                    out.append((node.value, node.lineno))
        return out

    def take(self, stem: str, value: ast.expr, func: Func | None, line: int) -> None:
        try:
            self.found |= self.resolve(stem, value, func, 0)
        except ValueError as why:
            self.unreadable.append(f"{self.files[stem].name}:{line}: {why}")

    def resolve(self, stem: str, node: ast.expr, func: Func | None, depth: int) -> set[str]:
        if depth > 8:
            raise ValueError("a reason chain too deep to follow")
        deeper = depth + 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Constant) and node.value is None:
            return set()
        if isinstance(node, ast.IfExp):
            return self.resolve(stem, node.body, func, deeper) | self.resolve(
                stem, node.orelse, func, deeper
            )
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.resolve(stem, v, func, deeper) for v in node.values))
        if isinstance(node, ast.Attribute):
            if node.attr == "reason":  # another reason, read from a decision that wrote it
                return set()
            if isinstance(node.value, ast.Name):
                module = self.module_alias(stem, node.value.id)
                if module is not None:
                    return self.constant(module, node.attr, deeper)
        if isinstance(node, ast.Name):
            if node.id.startswith("<"):
                raise ValueError(node.id[1:-1] + ": give the reason by keyword, or name it")
            local = self.local_values(func, node.id)
            if local:
                return set().union(*(self.resolve(stem, v, func, deeper) for v in local))
            if func is not None and func.name == "refuse" and node.id in params(func):
                return set()  # `refuse(self, step, reason)`: its callers are all read here
            return self.constant(stem, node.id, deeper)
        raise ValueError(f"a refusal reason the test cannot read: {ast.unparse(node)}")

    def local_values(self, func: Func | None, name: str) -> list[ast.expr]:
        if func is None:
            return []
        values: list[ast.expr] = []
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets
            ):
                values.append(n.value)
            if isinstance(n, ast.AnnAssign) and n.value is not None and is_name(n.target, name):
                values.append(n.value)
        return values

    def constant(self, stem: str, name: str, depth: int) -> set[str]:
        """A module-level `NAME = ...` here, or the one a `from x import NAME` names."""
        tree = self.trees.get(stem)
        if tree is None:
            raise ValueError(f"module {stem} is not in the package")
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets
            ):
                return self.resolve(stem, n.value, None, depth)
            if isinstance(n, ast.ImportFrom) and n.module is not None:
                for a in n.names:
                    if (a.asname or a.name) == name and n.module.startswith("scrimmage_core."):
                        return self.constant(n.module.rpartition(".")[2], a.name, depth)
        raise ValueError(f"a reason name the test cannot follow: {name}")

    def module_alias(self, stem: str, name: str) -> str | None:
        """`from scrimmage_core import decide as engine` makes `engine` the module `decide`."""
        for n in self.trees[stem].body:
            if isinstance(n, ast.ImportFrom) and n.module == "scrimmage_core":
                for a in n.names:
                    if (a.asname or a.name) == name and a.name in self.trees:
                        return a.name
        return None


def is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def params(func: Func) -> set[str]:
    a = func.args
    return {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


def refusal_reasons(package: Path | None = None) -> set[str]:
    """Every reason the gates write on a refused call, read from the code that writes it."""
    gates = Gates(package or Path(scrimmage_core.__file__).parent)
    assert not gates.unreadable, "\n".join(gates.unreadable)
    return gates.found


@pytest.mark.design("schema.record")
def test_every_reason_a_gate_can_write_is_in_the_record_schemas_list() -> None:
    reasons = refusal_reasons()
    # the walk saw them, the constant one (`mistake`) and the ones named through a local too
    assert {"orphan", "service_child", "budget_held", "unlisted", "mistake"} <= reasons
    body = cast(Any, schema("record"))
    listed = set(body["$defs"]["call"]["properties"]["reason"]["enum"])
    assert not reasons - listed, f"record.v1.json does not list {sorted(reasons - listed)}"
