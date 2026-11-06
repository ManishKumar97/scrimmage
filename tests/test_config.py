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
        ('do   = "timeout"', 'do   = "timeout"\nwhen = "once"', "'once' is not a schedule"),
        ('do   = "timeout"', 'do   = "slow 8s"', "'slow 8s' is not a fault"),
        ('do   = "timeout"', 'do   = "status 429"', "works only on a model call"),
        ('call = "tool:get_order"\ndo', 'call = "mcp:a/b"\ndo', "'mcp:a/b' is not a label"),
        ('call = "tool:get_order"\ndo', 'call = "model:x"\ndo', "not a known provider"),
        ('call = "tool:get_order"\ndo', 'call = "model:anthropic"\ndo', "not a known provider"),
        ('call = "tool:get_order"\ndo', 'call = "model:openai/images"\ndo', "not a model call"),
        ("[agent]\n", '[agent]\nanswer = "reply"\n', "answer: unknown setting; see docs"),
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
    assert example(tmp_path).agent.allowed_imports() == ["acme."]


def test_a_date_in_a_mock_is_rejected_because_a_plan_cannot_carry_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="a datetime cannot be sent to the agent"):
        example(tmp_path, 'returns = "shipped, Oct 30"', "returns = 1979-05-27T07:32:00Z")


def model_fault(do: str = "timeout", when: str = "always", call: str = "model") -> str:
    return f'[[test.fault]]\ncall = "{call}"\ndo   = "{do}"\nwhen = "{when}"\n'


FAULT = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'


@pytest.mark.design("config.model_settings")
def test_the_model_settings_and_their_defaults(tmp_path: Path) -> None:
    agent = example(
        tmp_path, "[agent]\n", "[agent]\nmax_cost_usd = 2.5\nreserve_output_tokens = 100\n"
    ).agent
    assert (agent.max_cost_usd, agent.reserve_output_tokens) == (2.5, 100)
    (tmp_path / "again").mkdir()
    default = example(tmp_path / "again").agent
    assert (default.max_cost_usd, default.reserve_output_tokens) == (5.0, 4096)


@pytest.mark.design("config.budget_off")
def test_the_budget_turns_off_on_purpose(tmp_path: Path) -> None:
    assert (
        example(tmp_path, "[agent]\n", '[agent]\nmax_cost_usd = "none"\n').agent.max_cost_usd
        is None
    )


def test_prices_and_model_hosts_are_read(tmp_path: Path) -> None:
    text = (
        (EXAMPLE / "faultline.toml")
        .read_text()
        .replace(
            "[[test]]\n",
            "[agent.prices]\nacme-llama = {input = 0.6, output = 0.7}\n\n[[test]]\n",
            1,
        )
    )
    agent = load_text(tmp_path, text).agent
    assert agent.prices == ((("acme-llama", config.Price(0.6, 0.7))),)
    assert agent.model_hosts == (config.ModelHost("127.0.0.1", "openai"),)


@pytest.mark.design("config.schedules")
@pytest.mark.parametrize(
    ("do", "when", "call"),
    [
        ("status 429", "first 3", "model"),
        ("status 429", "requests 1", "model"),
        ("timeout", "always", "model:openai"),
        ("status 500", "always", "model:openai/embeddings"),
        ("status 503", "always", "model:*/embeddings"),
    ],
)
def test_model_faults_and_schedules_are_read(tmp_path: Path, do: str, when: str, call: str) -> None:
    fault = example(tmp_path, FAULT, model_fault(do, when, call)).tests[0].faults[0]
    assert (fault.call, fault.do, fault.when) == (call, do, when)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (FAULT, model_fault("status 200"), "not an error status"),
        (FAULT, model_fault("status 429", "first 0"), "not a schedule"),
        (FAULT, model_fault("timeout", "requests 1", "tool:get_order"), "use 'first N' for a tool"),
        (FAULT, model_fault("timeout", "always", "model:acme"), "not a known provider"),
        (FAULT, model_fault("timeout", "always", "model:*"), "write 'model' for every provider"),
        (FAULT, model_fault("timeout", "always", "model:"), "is not of the form"),
        (FAULT, model_fault("timeout", "always", "hosted:mcp/orders"), "only under"),
        ("[agent]\n", "[agent]\nmax_cost_usd = 0\n", "expected dollars above 0"),
        ("[agent]\n", '[agent]\nmax_cost_usd = "lots"\n', "expected dollars above 0"),
        ("[agent]\n", "[agent]\nmax_cost_usd = true\n", "expected dollars above 0"),
        ("[agent]\n", "[agent]\nmax_cost_usd = nan\n", "expected dollars above 0"),
        ("[agent]\n", "[agent]\nmax_cost_usd = inf\n", "expected dollars above 0"),
        (
            'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
            'model_hosts = [{host = "gw:8000", provider = "openai"}]',
            "no port or path",
        ),
        (
            'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
            'model_hosts = [{host = "x"}]',
            "provider: expected text",
        ),
        (
            'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
            'model_hosts = [{host = "x", provider = "acme"}]',
            "not a known provider",
        ),
        (
            'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
            'model_hosts = [{host = "x", provider = "openai", port = 1}]',
            "port: unknown setting",
        ),
        (
            'real     = ["tool:check_inventory"]',
            'real = ["hosted:orders"]',
            "is not of the form",
        ),
    ],
)
def test_bad_model_settings_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        example(tmp_path, old, new)


@pytest.mark.parametrize(
    "table",
    [
        'acme = {input = "x", output = 1}',
        "acme = {input = 1}",
        "acme = {input = -1, output = 1}",
        "acme = {input = nan, output = 1}",
        "acme = {input = 1, output = inf}",
        "acme = {input = 1, output = 1, cached = 1}",
    ],
)
def test_bad_prices_are_rejected(tmp_path: Path, table: str) -> None:
    text = (
        (EXAMPLE / "faultline.toml")
        .read_text()
        .replace("[[test]]\n", f"[agent.prices]\n{table}\n\n[[test]]\n", 1)
    )
    with pytest.raises(ConfigError, match=r"\[agent\]\.prices acme"):
        load_text(tmp_path, text)


@pytest.mark.design("config.model_not_mockable")
def test_a_model_call_cannot_be_mocked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="model cannot be mocked"):
        example(tmp_path, 'call    = "tool:get_order"', 'call = "model"')


@pytest.mark.design("config.model_not_mockable")
def test_a_model_call_is_not_listed_as_real(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="real: model is not needed"):
        example(tmp_path, 'real     = ["tool:check_inventory"]', 'real = ["model"]')
