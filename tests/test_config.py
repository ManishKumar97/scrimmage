from pathlib import Path

import pytest

from conftest import EXAMPLE
from faultline_sdk import config, plan
from faultline_sdk.errors import ConfigError


def load_text(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "faultline.toml"
    path.write_text(text)
    return config.load(path)


def example(tmp_path: Path, old: str = "", new: str = "") -> config.Config:
    text = (EXAMPLE / "faultline.toml").read_text()
    assert old in text
    return load_text(tmp_path, text.replace(old, new, 1) if old else text)


@pytest.mark.design("plan.rows")
def test_the_example_expands_to_two_rows_and_six_runs(tmp_path: Path) -> None:
    cfg = example(tmp_path)
    rows = plan.rows(cfg)
    assert [(r.name, r.fault) for r in rows] == [
        ("baseline", None),
        ("fault 1", config.Fault("tool:get_order", "timeout", "always")),
    ]
    assert len(rows) * cfg.agent.runs == 6


@pytest.mark.design("config.defaults")
def test_defaults(tmp_path: Path) -> None:
    agent = example(tmp_path).agent
    assert (agent.runs, agent.parallel, agent.run_timeout) == (3, 4, 120.0)


@pytest.mark.design("plan.mocks_on_every_row")
def test_every_row_uses_the_mocks_and_a_fault_row_adds_one_fault(
    tmp_path: Path,
) -> None:
    cfg = example(tmp_path)
    base, fault = (plan.plan_for(cfg, row, f"run-{i}") for i, row in enumerate(plan.rows(cfg)))
    assert base.mocks == fault.mocks and len(base.mocks) == 2
    assert (base.faults, len(fault.faults)) == ((), 1)
    assert base.time_limit_s == cfg.agent.run_timeout


@pytest.mark.design("config.names_table_and_key")
def test_a_bad_value_names_its_table_and_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[agent\] run_timeout: expected a number"):
        example(tmp_path, "[agent]\n", '[agent]\nrun_timeout = "abc"\n')


@pytest.mark.design("config.unknown_rejected")
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('returns = "shipped, Oct 30"', 'returns = "${X}"', "placeholders are not read"),
        ('do   = "timeout"', 'do   = "timeout"\nwhen = "first 2"', "'first 2' is not a schedule"),
        ('do   = "timeout"', 'do   = "status 429"', "'status 429' is not a fault"),
        ('call = "tool:get_order"\ndo', 'call = "model"\ndo', "'model' is not a label"),
        ('call = "tool:get_order"\ndo', 'call = "model:x"\ndo', "'model:x' is not a label"),
        ("[agent]\n", "[agent]\nmax_cost_usd = 1\n", "max_cost_usd: unknown setting; see docs"),
        ("[agent]\n", '[agent]\ncommand = "python bot.py"\n', "command: unknown setting"),
        ("[agent]\n", "[agent]\nrnus = 50\n", "rnus: unknown setting"),
        ('returns = "shipped, Oct 30"', 'returns_from = "acme.m:f"', "returns_from: unknown"),
        ('do   = "timeout"', 'do   = "timeout"\nraises = "X"', "raises: unknown setting"),
    ],
)
def test_anything_this_version_does_not_support_is_rejected(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        example(tmp_path, old, new)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("[agent]\n", "[agent]\ncolour = 1\n", r"\[agent\] colour: unknown setting"),
        (
            'function = "acme.agent:answer"',
            'function = "acme.agent"',
            "not an import path",
        ),
        ('function = "acme.agent:answer"', "", r"\[agent\] function: required"),
        ('expect   = ["shipped", "Oct 30"]', "", "expect: required"),
        ('expect   = ["shipped", "Oct 30"]', "expect = []", "expected a list of text"),
        ('returns = "shipped, Oct 30"', "", "returns: required"),
        ('do   = "timeout"', 'do   = "explode"', "'explode' is not a fault"),
        ('do   = "timeout"', 'do   = "timeout"\nwhen = "sometimes"', "not a schedule"),
        ('call    = "tool:get_order"', 'call = "get_order"', "is not a label"),
        ('call    = "tool:get_order"', 'call = "tool:*"', "allowed only under"),
        ('call    = "tool:get_order"', 'call = "tool:get order"', "is not a tool name"),
        ('real     = ["tool:check_inventory"]', "real = [1]", "expected labels"),
        ('real     = ["tool:check_inventory"]', "real = 1", "expected a list"),
        (
            '"acme.tools:get_stock"]',
            '"acme.tools:get_stock", "acme.more:get_stock"]',
            "two tools",
        ),
        ('name     = "order-status"', "name = 3", "expected text"),
        ('"acme.tools:get_stock"]', '{path = "acme.tools:get_stock"}]', "name: expected text"),
        ('"acme.tools:get_stock"]', '{path = "a:b", name = "no spaces"}]', "is not a tool name"),
        ('"acme.tools:get_stock"]', '{path = "a:b", name = "x", z = 1}]', "z: unknown setting"),
        ('"acme.tools:get_stock"]', '{path = "a:b", name = "get_order"}]', "two tools"),
        ('"acme.tools:get_stock"]', "3]", "expected a table"),
        ("[agent]\n", "[agent]\nruns = 0\n", "runs: expected a whole number"),
        (
            "[agent]\n",
            "[agent]\nparallel = true\n",
            "parallel: expected a whole number",
        ),
        ("[[test]]\n", "[[test]]\nother = 1\n", "other: unknown setting"),
        (
            'returns = {sku = "SKU-42", count = 12}',
            "type = 3\nreturns = 1",
            "type: expected text",
        ),
    ],
)
def test_bad_files_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        example(tmp_path, old, new)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[agent", "faultline.toml"),
        (
            '[[test]]\nname = "a"\nquestion = "q"\nexpect = ["x"]',
            r"\[agent\] is required",
        ),
        ('[agent]\nfunction = "a:b"', r"at least one \[\[test\]\]"),
        ('top = 1\n[agent]\nfunction = "a:b"', "top: unknown setting"),
        (
            'agent = 1\n[[test]]\nname = "a"\nquestion = "q"\nexpect = ["x"]',
            "expected a table",
        ),
        (
            '[agent]\nfunction = "a:b"\n'
            + '[[test]]\nname = "a"\nquestion = "q"\nexpect = ["x"]\n' * 2,
            "name is used twice",
        ),
    ],
)
def test_bad_layouts_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_text(tmp_path, text)


def test_allowed_imports_are_the_agents_own_packages(tmp_path: Path) -> None:
    assert example(tmp_path).allowed_imports() == ["acme."]


def test_a_date_in_a_mock_is_rejected_because_a_plan_cannot_carry_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="a datetime cannot be sent to the agent"):
        example(tmp_path, 'returns = "shipped, Oct 30"', "returns = 1979-05-27T07:32:00Z")
