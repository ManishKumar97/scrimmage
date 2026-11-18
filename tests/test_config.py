from pathlib import Path

import pytest

import config_cases
from conftest import EXAMPLE
from scrimmage_core import config, plan
from scrimmage_core.errors import ConfigError
from scrimmage_core.provider_openai import Rates


def load_text(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "scrimmage.toml"
    path.write_text(text)
    return config.load(path)


def example(tmp_path: Path, old: str = "", new: str = "") -> config.Config:
    text = (EXAMPLE / "scrimmage.toml").read_text()
    assert old in text
    return load_text(tmp_path, text.replace(old, new, 1) if old else text)


@pytest.mark.design("plan.rows")
def test_the_example_expands_to_two_rows_and_six_runs(tmp_path: Path) -> None:
    cfg = example(tmp_path)
    rows = plan.rows(cfg)
    assert [(r.name, r.fault) for r in rows] == [
        ("baseline", None),
        (
            "fault 1",
            config.Fault(
                "tool:get_order",
                "timeout",
                "always",
                expect=(("unable", "sorry", "try again"),),
                forbid=("Oct 30", "October 30"),
            ),
        ),
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
        ('returns = "shipped, Oct 30"', 'returns = "${NOT_SET_X}"', r"\$\{NOT_SET_X\} is not set"),
        ('do   = "timeout"', 'do   = "timeout"\nwhen = "once"', "'once' is not a schedule"),
        ('do   = "timeout"', 'do   = "slow 8"', "'slow 8' is not a delay"),
        ('do   = "timeout"', 'do   = "status 429"', "works on a model call or a web request"),
        ('call = "tool:get_order"\ndo', 'call = "auth:a"\ndo', "'auth:a' is not a label"),
        ('call = "tool:get_order"\ndo', 'call = "model:x"\ndo', "not a known provider"),
        ('call = "tool:get_order"\ndo', 'call = "model:anthropic"\ndo', "not a known provider"),
        ('call = "tool:get_order"\ndo', 'call = "model:openai/images"\ndo', "not a model call"),
        ("[agent]\n", '[agent]\nrecords = "x"\n', "records: unknown setting"),
        ("[agent]\n", "[agent]\nrnus = 50\n", "rnus: unknown setting"),
        ('returns = "shipped, Oct 30"', 'returns_from = "acme.m:f"', "returns_from: unknown"),
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
        ('function = "acme.agent:answer"', "", r"\[agent\]: give function, command or service"),
        (
            'function = "acme.agent:answer"',
            'function = "acme.agent:answer"\ncommand = "python bot.py {question}"',
            r"\[agent\]: give function, command or service",
        ),
        (
            'function = "acme.agent:answer"',
            'command = "python bot.py"',
            r"must hold \{question\}",
        ),
        ('function = "acme.agent:answer"', 'command = ""', "command: expected text"),
        (
            'function = "acme.agent:answer"',
            """command = "python 'bot.py {question}\"""",
            "command: No closing quotation",
        ),
        (
            'function = "acme.agent:answer"',
            'command = "python bot.py {question}"\nanswer = "reply"',
            "answer: works only with function",
        ),
        ("[agent]\n", '[agent]\nchildren = "yes"\n', "children: expected true or false"),
        ('expect   = ["shipped", ["Oct 30", "October 30"]]', "", "expect: required"),
        (
            'expect   = ["shipped", ["Oct 30", "October 30"]]',
            "expect = []",
            "expected a list of text",
        ),
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
        ("[agent", "scrimmage.toml"),
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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('do   = "timeout"', 'do   = "timeout"\nraises = "ValueError"', "only with do = 'error'"),
        ('do   = "timeout"', 'do   = "error"\nraises = "NotAnError"', "not a built-in exception"),
        ('do   = "timeout"', 'do   = "error"\nraises = "print"', "not a built-in exception"),
        ('do   = "timeout"', 'do   = "error"\nraises = "UnicodeDecodeError"', "not a built-in"),
        ('do   = "timeout"', 'do   = "error"\nraises = "ExceptionGroup"', "not a built-in"),
        (
            'do   = "timeout"',
            'do   = "error"\nraises = "other.m:E"',
            "outside the agent's packages",
        ),
        (
            'call = "tool:get_order"\ndo   = "timeout"',
            'call = "model"\ndo = "error"',
            "only on a tool",
        ),
        (
            "[agent]\n",
            '[agent]\nanswer = "messages[x]"\n',
            r"answer: 'messages\[x\]' is not a path",
        ),
        ("[agent]\n", "[agent]\nanswer = 3\n", "answer: 3 is not a path"),
    ],
)
def test_the_error_fault_and_the_answer_path_are_checked(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        example(tmp_path, old, new)


@pytest.mark.design("config.error_raises_forms")
def test_raises_takes_a_built_in_or_the_agents_own_class(tmp_path: Path) -> None:
    built_in = example(tmp_path, 'do   = "timeout"', 'do   = "error"\nraises = "ConnectionError"')
    assert built_in.tests[0].faults[0].raises == "ConnectionError"
    own = example(tmp_path, 'do   = "timeout"', 'do   = "error"\nraises = "acme.errors:Down"')
    assert own.tests[0].faults[0].raises == "acme.errors:Down"


@pytest.mark.design("config.no_tools_list_defers_names")
def test_without_a_tools_list_tool_names_are_left_to_the_run(tmp_path: Path) -> None:
    text = (EXAMPLE / "scrimmage.toml").read_text()
    line = next(x for x in text.splitlines() if x.startswith("tools "))
    cfg = load_text(tmp_path, text.replace(line, "").replace("tool:get_order", "tool:anything"))
    assert cfg.agent.tools == ()


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
        (EXAMPLE / "scrimmage.toml")
        .read_text()
        .replace(
            "[[test]]\n",
            "[agent.prices]\nacme-llama = {input = 0.6, output = 0.7}\n\n[[test]]\n",
            1,
        )
    )
    agent = load_text(tmp_path, text).agent
    assert agent.prices == ((("acme-llama", Rates(0.6, 0.7))),)
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


@pytest.mark.design("config.call_n")
@pytest.mark.parametrize(
    ("call", "do"),
    [
        ("tool:get_order", "timeout"),
        ("mcp:search", "error"),
        ("http:orders.acme.internal", "status 503"),
        ("model", "status 429"),
        ("model:openai/embeddings", "timeout"),
    ],
)
def test_call_n_is_read_on_every_kind(tmp_path: Path, call: str, do: str) -> None:
    text = f'[[test.fault]]\ncall = "{call}"\ndo = "{do}"\nwhen = "call 3"\n'
    fault = with_fault(tmp_path, text).tests[0].faults[-1]
    assert (fault.call, fault.do, fault.when) == (call, do, "call 3")
    sent = plan.Plan("run-1", "t", "fault 1", (), (fault,), (), (), 60.0).to_json()
    assert plan.Plan.from_json(sent).faults == (config.Fault(call, do, "call 3"),)  # SDK runs it


@pytest.mark.design("config.call_n_judged_by_the_test")
def test_call_n_needs_no_row_expect(tmp_path: Path) -> None:
    """The agent gets the data on every other call, so the test's expect can judge the row."""
    text = '[[test.fault]]\ncall = "tool:get_order"\ndo = "timeout"\nwhen = "{when}"\n'
    fault = with_fault(tmp_path, text.format(when="call 1")).tests[0].faults[-1]
    assert (fault.when, fault.expect, fault.forbid) == ("call 1", None, None)
    with pytest.raises(ConfigError, match="this fault fires on every call"):
        with_fault(tmp_path, text.format(when="always"))  # the positive control


@pytest.mark.design("config.once_refused")
@pytest.mark.parametrize("when", ['"once"', "3", '"3"'])
def test_once_is_refused_naming_first_1(tmp_path: Path, when: str) -> None:
    text = f'[[test.fault]]\ncall = "tool:get_order"\ndo = "timeout"\nwhen = {when}\n'
    with pytest.raises(ConfigError, match="is not a schedule") as raised:
        with_fault(tmp_path, text)
    said = str(raised.value)
    assert "write 'first 1' for the first call alone" in said, said
    assert "'call N' for the Nth call alone" in said, said


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (FAULT, model_fault("status 200"), "not an error status"),
        (FAULT, model_fault("status 429", "first 0"), "not a schedule"),
        (FAULT, model_fault("timeout", "requests 1", "tool:get_order"), "use 'first N' here"),
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
        (EXAMPLE / "scrimmage.toml")
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


MODEL_FAULT = """
[[test.fault]]
call = "{call}"
model = {model}
do = "status 429"
when = "first 3"
"""


def with_fault(tmp_path: Path, text: str) -> config.Config:
    return load_text(tmp_path, (EXAMPLE / "scrimmage.toml").read_text() + text)


@pytest.mark.design("config.model_on_model_labels")
@pytest.mark.parametrize(
    "call", ["model", "model:openai", "model:openai/embeddings", "model:*/embeddings"]
)
def test_a_model_fault_can_name_a_model(tmp_path: Path, call: str) -> None:
    text = MODEL_FAULT.format(call=call, model='"gpt-4.1"')
    fault = with_fault(tmp_path, text).tests[0].faults[-1]
    assert (fault.model, fault.target) == ("gpt-4.1", f"{call} gpt-4.1")


@pytest.mark.design("config.model_any_alias")
def test_a_gateways_alias_is_a_name(tmp_path: Path) -> None:
    text = MODEL_FAULT.format(call="model", model='"team/smart:v2"')
    assert with_fault(tmp_path, text).tests[0].faults[-1].model == "team/smart:v2"


@pytest.mark.design("config.model_not_on_other_labels")
@pytest.mark.parametrize("call", ["tool:get_order", "http:orders.acme.internal", "mcp:search"])
def test_model_on_another_kind_of_fault_is_unknown_with_a_pointer(
    tmp_path: Path, call: str
) -> None:
    text = f'[[test.fault]]\ncall = "{call}"\nmodel = "gpt-4.1"\ndo = "timeout"\nwhen = "first 1"\n'
    with pytest.raises(ConfigError, match=r"model: unknown setting; see docs/configuration.md"):
        with_fault(tmp_path, text)


@pytest.mark.design("config.model_not_on_mocks")
def test_model_on_a_mock_is_unknown_with_a_pointer(tmp_path: Path) -> None:
    text = '[[test.mock]]\ncall = "tool:get_stock"\nreturns = 1\nmodel = "gpt-4.1"\n'
    with pytest.raises(ConfigError, match=r"model: unknown setting; see docs/configuration.md"):
        with_fault(tmp_path, text)


@pytest.mark.design("config.model_bad_value")
@pytest.mark.parametrize("value", ["5", "true", '""', '"  "', '["gpt-4.1"]'])
def test_a_model_that_is_not_text_is_rejected_by_table_and_key(tmp_path: Path, value: str) -> None:
    with pytest.raises(
        ConfigError,
        match=r"\[\[test\.fault\]\] 2 of .* model: .* is not a model name.*docs/model-faults.md",
    ):
        with_fault(tmp_path, MODEL_FAULT.format(call="model", model=value))


@pytest.mark.design("config.model_cannot_match")
@pytest.mark.parametrize(
    "value", ['"gpt-*"', '"*"', '" gpt-4.1"', '"gpt-4.1 "', '"gpt\\n4.1"', '"gpt\\u001b[31m"']
)
def test_a_name_that_could_never_match_stops_before_any_model_call(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigError, match=r"is not a model name.*docs/model-faults.md"):
        with_fault(tmp_path, MODEL_FAULT.format(call="model", model=value))


@pytest.mark.design("plan.model_round_trip")
def test_the_name_survives_the_plan_so_a_fault_cannot_widen_on_the_way(tmp_path: Path) -> None:
    cfg = with_fault(tmp_path, MODEL_FAULT.format(call="model", model='"gpt-4.1"'))
    row = plan.rows(cfg)[-1]
    sent = plan.plan_for(cfg, row, "run-1").to_json()
    assert sent["faults"] == [
        {"call": "model", "do": "status 429", "when": "first 3", "model": "gpt-4.1"}
    ]
    assert plan.Plan.from_json(sent).faults[0].model == "gpt-4.1"


@pytest.mark.design("plan.no_model_no_key")
def test_a_fault_with_no_model_sends_none() -> None:
    assert "model" not in plan.fault_json(config.Fault("model", "timeout", "first 1"))


@pytest.mark.design("config.impossible_row_names_the_model")
def test_a_row_no_answer_can_pass_is_named_with_its_model(tmp_path: Path) -> None:
    text = (
        '[[test.fault]]\ncall = "model"\nmodel = "gpt-4.1"\ndo = "status 429"\n'
        'expect = ["a"]\nforbid = ["a"]\n'
    )
    with pytest.raises(ConfigError, match=r"fault on model gpt-4\.1: no answer can pass"):
        with_fault(tmp_path, text)


@pytest.mark.design("budget.snapshot_prices")
def test_a_snapshot_priced_apart_from_its_family_has_its_own_row() -> None:
    from scrimmage_core import provider_openai as table

    assert table.price("gpt-4o-2024-05-13", {}) == Rates(5.00, 15.00)  # not gpt-4o's $2.50
    for snapshot in ("gpt-4o-2024-08-06", "gpt-4o-2024-11-20"):
        assert table.price(snapshot, {}) == table.PRICES["gpt-4o"]
    assert table.price("gpt-4o-mini-2024-07-18", {}) == table.PRICES["gpt-4o-mini"]
    assert table.price("gpt-4.1-2025-04-14", {}) == table.PRICES["gpt-4.1"]
    assert table.price("gpt-4.1-mini-2025-04-14", {}) == table.PRICES["gpt-4.1-mini"]


@pytest.mark.design("config.recorded_setting")
def test_recorded_and_args_are_read_and_carried_by_the_plan(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, config_cases.RECORDED)
    tool, mcp, web, hand = cfg.tests[0].mocks
    assert (tool.recorded, tool.args) == (True, {"order_id": "1234"})
    assert (mcp.recorded, mcp.args) == (True, {"q": "refunds"})
    assert (web.recorded, web.args) == (True, None)
    assert (hand.recorded, hand.args) == (False, None)
    sent = plan.plan_for(cfg, plan.rows(cfg)[0], "run-1")
    assert plan.Plan.from_json(sent.to_json()).mocks == sent.mocks
    assert "recorded" not in plan.mock_json(hand)


@pytest.mark.design("config.recorded_setting")
@pytest.mark.parametrize("value", ['"yes"', "1", "[true]"])
def test_a_recorded_value_that_is_not_true_or_false_is_rejected(tmp_path: Path, value: str) -> None:
    text = config_cases.mock(f'call = "tool:t"\nreturns = 1\nrecorded = {value}\n')
    with pytest.raises(ConfigError, match=r"\[\[test.mock\]\] 1 of 't' recorded: expected true"):
        load_text(tmp_path, text)


@pytest.mark.design("config.args_only_recorded")
@pytest.mark.parametrize("recorded", ["", "recorded = false\n"])
def test_args_on_a_hand_written_mock_are_read(tmp_path: Path, recorded: str) -> None:
    text = config_cases.mock(f'call = "tool:t"\nreturns = 1\n{recorded}args = {{a = 1}}\n')
    mock = load_text(tmp_path, text).tests[0].mocks[0]
    assert (mock.recorded, mock.args) == (False, {"a": 1})


@pytest.mark.design("config.args_only_recorded")
def test_args_that_are_not_a_table_are_rejected(tmp_path: Path) -> None:
    text = config_cases.mock('call = "tool:t"\nreturns = 1\nrecorded = true\nargs = "a"\n')
    with pytest.raises(ConfigError, match="args: expected a table of the call's arguments"):
        load_text(tmp_path, text)


@pytest.mark.design("config.args_only_recorded")
def test_a_web_mock_with_args_is_still_rejected(tmp_path: Path) -> None:
    web = config_cases.mock('call = "http:a.acme"\nrecorded = true\nargs = {a = 1}\n')
    with pytest.raises(ConfigError, match="args: unknown setting"):
        load_text(tmp_path, web)


@pytest.mark.design("config.args_only_recorded")
@pytest.mark.parametrize(
    "value", ["2026-11-10", "2026-11-10T09:00:00Z", "09:00:00", "[1, 2026-11-10]", "{d = 09:00:00}"]
)
def test_a_date_in_args_is_rejected(tmp_path: Path, value: str) -> None:
    text = config_cases.mock(f'call = "tool:t"\nreturns = 1\nargs = {{when = {value}}}\n')
    with pytest.raises(ConfigError, match=r"args\.when.*cannot be sent to the agent"):
        load_text(tmp_path, text)


CATCH_ALL = '[[test.mock]]\ncall = "{}"\nreturns = 1\n'
WITH_ARGS = '[[test.mock]]\ncall = "{}"\nreturns = 2\nargs = {{a = 1}}\n'


@pytest.mark.design("config.args_only_recorded")
@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        ("tool:t", "tool:t"),
        ("mcp:search", "mcp:docs/search"),
        ("mcp:docs/search", "mcp:docs/search"),
    ],
)
def test_a_catch_all_above_an_args_mock_on_its_label_is_a_mistake(
    tmp_path: Path, upper: str, lower: str
) -> None:
    text = config_cases.test("\n" + CATCH_ALL.format(upper) + "\n" + WITH_ARGS.format(lower))
    with pytest.raises(ConfigError, match="has no args, so it answers every call"):
        load_text(tmp_path, text)


@pytest.mark.design("config.args_only_recorded")
def test_an_args_mock_above_a_catch_all_is_legal(tmp_path: Path) -> None:
    text = config_cases.test("\n" + WITH_ARGS.format("tool:t") + "\n" + CATCH_ALL.format("tool:t"))
    assert len(load_text(tmp_path, text).tests[0].mocks) == 2
    both = config_cases.test("\n" + WITH_ARGS.format("tool:t") + "\n" + WITH_ARGS.format("tool:t"))
    assert len(load_text(tmp_path, both).tests[0].mocks) == 2  # what record writes
    other = config_cases.test("\n" + CATCH_ALL.format("tool:u") + "\n" + WITH_ARGS.format("tool:t"))
    assert len(load_text(tmp_path, other).tests[0].mocks) == 2


@pytest.mark.design("config.args_only_recorded")
@pytest.mark.parametrize("key", ["ignore", "match", "pattern"])
def test_ignore_match_and_pattern_are_unknown_keys(tmp_path: Path, key: str) -> None:
    text = config_cases.mock(f'call = "tool:t"\nreturns = 1\nargs = {{a = 1}}\n{key} = ["a"]\n')
    with pytest.raises(ConfigError, match=f"{key}: unknown setting"):
        load_text(tmp_path, text)


# A carried fault on a tool the test mocks, with the keys each test then changes.
CARRIED = '[[test.fault]]\ncall = "tool:get_order"\ndo = "{do}"\n{extra}when = "first 1"\n'


def carried_fault(do: str, extra: str = "") -> str:
    return CARRIED.format(do=do, extra=extra)


@pytest.mark.parametrize("do", ["append", "replace"])
def test_append_and_replace_need_text(tmp_path: Path, do: str) -> None:
    with pytest.raises(ConfigError, match=rf"text: required with do = '{do}'"):
        with_fault(tmp_path, carried_fault(do))
    fault = with_fault(tmp_path, carried_fault(do, 'text = "x"\n')).tests[0].faults[-1]
    assert (fault.do, fault.text, fault.find) == (do, "x", None)


@pytest.mark.parametrize("do", ["timeout", "error", "slow 2s"])
def test_text_and_find_work_only_with_append_and_replace(tmp_path: Path, do: str) -> None:
    for key in ("text", "find"):
        with pytest.raises(ConfigError, match=rf"{key}: works only with do = 'append' or 'repl"):
            with_fault(tmp_path, carried_fault(do, f'{key} = "x"\n'))


def test_find_works_only_with_replace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"find: works only with do = 'replace'"):
        with_fault(tmp_path, carried_fault("append", 'text = "x"\nfind = "y"\n'))
    fault = with_fault(tmp_path, carried_fault("replace", 'text = "x"\nfind = "y"\n'))
    assert fault.tests[0].faults[-1].find == "y"
    with pytest.raises(ConfigError, match=r"find: expected text that is not blank"):
        with_fault(tmp_path, carried_fault("replace", 'text = "x"\nfind = "  "\n'))


def test_empty_replace_text_needs_find(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"text: an empty text replaces the whole result"):
        with_fault(tmp_path, carried_fault("replace", 'text = ""\nforbid = ["a"]\n'))
    kept = with_fault(tmp_path, carried_fault("replace", 'text = ""\nfind = "y"\n'))
    assert kept.tests[0].faults[-1].text == ""
    with pytest.raises(ConfigError, match=r"text: blank text adds nothing"):
        with_fault(tmp_path, carried_fault("append", 'text = "  "\n'))
    with pytest.raises(ConfigError, match=r"text: expected text, got 5"):
        with_fault(tmp_path, carried_fault("append", "text = 5\n"))


@pytest.mark.parametrize("call", ["model", "model:openai", "model:*/embeddings"])
def test_append_is_refused_on_a_model_call(tmp_path: Path, call: str) -> None:
    """replace and slow work on a chat call; append never does, and a call that only reads
    takes neither."""
    text = f'[[test.fault]]\ncall = "{call}"\ndo = "append"\ntext = "x"\nforbid = ["a"]\n'
    with pytest.raises(ConfigError, match=r"append does not work on a model call.*model-faults"):
        with_fault(tmp_path, text)
    with pytest.raises(ConfigError, match=r"text: works only with do = 'append' or 'replace'"):
        with_fault(tmp_path, text.replace("append", "timeout"))
    for do in ("replace", "slow 3s"):
        if "/" in call:
            with pytest.raises(ConfigError, match=rf"{do.split()[0]} works on a chat call"):
                with_fault(tmp_path, text.replace("append", do) + 'model = "m"\n')


def test_append_always_needs_no_row_expect(tmp_path: Path) -> None:
    text = '[[test.fault]]\ncall = "tool:get_order"\ndo = "append"\ntext = "x"\n'
    assert with_fault(tmp_path, text).tests[0].faults[-1].when == "always"
    with_find = text.replace("append", "replace") + 'find = "y"\n'
    assert with_fault(tmp_path, with_find).tests[0].faults[-1].find == "y"


@pytest.mark.parametrize(
    "text",
    [
        '[[test.fault]]\ncall = "tool:get_order"\ndo = "replace"\ntext = "x"\n',
        '[[test.fault]]\ncall = "tool:get_order"\ndo = "slow 2s"\n',
    ],
)
def test_a_whole_replace_always_needs_a_row_expect(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match=r"fires on every call, so the agent never gets the data"):
        with_fault(tmp_path, text)
    assert with_fault(tmp_path, text + 'forbid = ["a"]\n')


def test_slow_takes_seconds_under_run_timeout(tmp_path: Path) -> None:
    fault = with_fault(tmp_path, carried_fault("slow 0.5s")).tests[0].faults[-1]
    assert config.slow_seconds(fault.do) == 0.5
    for bad, message in (
        ("slow 0s", "must be above 0"),
        ("slow 601s", "at most 600"),
        ("slow 8", "is not a delay"),
        ("slow 8 s", "is not a delay"),
        ("slow -1s", "is not a delay"),
        ("slow", "is not a delay"),
    ):
        with pytest.raises(ConfigError, match=message):
            with_fault(tmp_path, carried_fault(bad, 'forbid = ["a"]\n'))
    with pytest.raises(ConfigError, match=r"slow 130s is not under run_timeout, 120"):
        with_fault(tmp_path, carried_fault("slow 130s", 'forbid = ["a"]\n'))
    with pytest.raises(ConfigError, match=r"slow 120s is not under run_timeout, 120"):
        with_fault(tmp_path, carried_fault("slow 120s", 'forbid = ["a"]\n'))
    assert with_fault(tmp_path, carried_fault("slow 119s", 'forbid = ["a"]\n'))


RETRY = '[[test.fault]]\ncall = "{call}"\ndo = "{do}"\nwhen = "first 1"\nretry_after = {wait}\n'


def retry_fault(do: str, wait: object = 30, call: str = "model") -> str:
    return RETRY.format(call=call, do=do, wait=wait)


@pytest.mark.design("config.retry_after_statuses")
def test_retry_after_works_only_on_status_429_and_503(tmp_path: Path) -> None:
    """RFC 9110 and RFC 6585 give the header to a 503 and a 429; on a 400 it means nothing to
    a library, so it is refused there instead of sent for nothing."""
    for call in ("model", "model:openai/embeddings", "http:inventory.acme.internal"):
        for do in config.RETRY_AFTER:
            fault = with_fault(tmp_path, retry_fault(do, call=call)).tests[0].faults[-1]
            assert (fault.do, fault.retry_after) == (do, 30)
    for do in ("status 500", "status 400", "status 502", "timeout", "refused", "bad body"):
        with pytest.raises(ConfigError, match=r'retry_after: works only with do = "status 429"'):
            with_fault(tmp_path, retry_fault(do))
    # A tool's reply has no headers, so the key is not one a tool fault has.
    with pytest.raises(ConfigError, match="retry_after: unknown setting"):
        with_fault(tmp_path, retry_fault("timeout", call="tool:get_order"))
    with pytest.raises(ConfigError, match="retry_after: unknown setting"):
        with_fault(tmp_path, retry_fault("timeout", call="mcp:docs/search"))
    assert with_fault(tmp_path, model_fault_without_wait()).tests[0].faults[-1].retry_after is None


def model_fault_without_wait() -> str:
    return '[[test.fault]]\ncall = "model"\ndo = "status 429"\nwhen = "first 1"\n'


@pytest.mark.design("config.retry_after_seconds")
def test_retry_after_is_whole_seconds_under_run_timeout(tmp_path: Path) -> None:
    """The header's delay form is whole seconds, and a wait the run cannot outlast is a
    mistake, as for slow."""
    for wait in (0, 1, 119):
        fault = with_fault(tmp_path, retry_fault("status 429", wait)).tests[0].faults[-1]
        assert fault.retry_after == wait
    for bad in ("-1", "2.5", '"30"', '"30s"', "true", "1e3"):
        with pytest.raises(ConfigError, match="retry_after: expected whole seconds from 0"):
            with_fault(tmp_path, retry_fault("status 429", bad))
    # The example's run_timeout is the default, 120.
    for wait in (120, 130):
        with pytest.raises(ConfigError, match=rf"retry_after {wait} is not under run_timeout, 120"):
            with_fault(tmp_path, retry_fault("status 503", wait))
    longer = (
        (EXAMPLE / "scrimmage.toml")
        .read_text()
        .replace("[agent]\n", "[agent]\nrun_timeout = 200\n", 1)
    )
    loaded = load_text(tmp_path, longer + retry_fault("status 429", 130))
    assert loaded.tests[0].faults[-1].retry_after == 130


NOT_COVERED = '[[test.fault]]\ncall = "{call}"\ndo = "append"\ntext = "x"\n{extra}'


@pytest.mark.parametrize(
    ("call", "extra"),
    [
        ("tool:refund_order", ""),  # neither mocked nor real
        ("mcp:docs/search", ""),
        ("http:orders.acme.internal", 'method = "POST"\n'),
    ],
)
def test_a_carried_fault_on_a_tool_nothing_lets_run_is_a_problem(
    tmp_path: Path, call: str, extra: str
) -> None:
    text = NOT_COVERED.format(call=call, extra=extra)
    with pytest.raises(
        ConfigError, match=r"append lets the call run, so .* must be mocked or listed"
    ):
        with_fault(tmp_path, text)
    with pytest.raises(ConfigError, match="must be mocked or listed"):
        with_fault(
            tmp_path, text.replace("append", "slow 1s").replace('text = "x"\n', 'forbid = ["a"]\n')
        )


@pytest.mark.parametrize(
    ("agent_real", "mocks", "call", "extra"),
    [
        ('real = ["tool:*"]', "", "tool:refund_order", ""),
        ('real = ["tool:refund_order"]', "", "tool:refund_order", ""),
        (
            "",
            '[[test.mock]]\ncall = "tool:refund_order"\nreturns = "ok"\n',
            "tool:refund_order",
            "",
        ),
        ('real = ["mcp:search"]', "", "mcp:docs/search", ""),
        ("", '[[test.mock]]\ncall = "mcp:search"\nreturns = "ok"\n', "mcp:docs/search", ""),
        ("", "", "http:orders.acme.internal", ""),  # a web read runs on its own
        ("", "", "http:orders.acme.internal", 'method = "GET"\n'),
        (
            'real = ["http:orders.acme.internal POST"]',
            "",
            "http:orders.acme.internal",
            'method = "POST"\n',
        ),
    ],
)
def test_a_carried_fault_under_tool_star_is_allowed(
    tmp_path: Path, agent_real: str, mocks: str, call: str, extra: str
) -> None:
    text = (
        (EXAMPLE / "scrimmage.toml")
        .read_text()
        .replace('real     = ["tool:check_inventory"]', agent_real)
    )
    text += mocks + NOT_COVERED.format(call=call, extra=extra)
    assert load_text(tmp_path, text).tests[0].faults[-1].call == call


def warned(tmp_path: Path, real: str, mocks: str = "") -> list[str]:
    text = (
        (EXAMPLE / "scrimmage.toml")
        .read_text()
        .replace('real     = ["tool:check_inventory"]', f"real = {real}")
    )
    text += mocks + NOT_COVERED.format(call="tool:check_inventory", extra="")
    return config.write_warnings(load_text(tmp_path, text))


def test_an_append_row_warns_which_writes_could_run_for_real(tmp_path: Path) -> None:
    [warning] = warned(
        tmp_path,
        '["tool:*", "http:orders.acme.internal POST /refunds", "http:a.acme.internal", '
        '"http:r.acme.internal GET", "mcp:orders/update"]',
    )
    assert warning == (
        "fault 2 of 'order-status' adds text the agent may obey; these would run for real "
        "if it did: every tool not mocked, under tool:* (check_inventory, refund_order); "
        "http:orders.acme.internal POST /refunds; http:a.acme.internal; mcp:orders/update. "
        "Leave a write unmocked and off real to see it refused instead"
    )


def test_an_append_row_with_no_listed_write_has_no_warning(tmp_path: Path) -> None:
    covered = '[[test.mock]]\ncall = "tool:check_inventory"\nreturns = "x"\n'
    assert warned(tmp_path, '["http:r.acme.internal GET"]', covered) == []
    # Every tool mocked: tool:* names none.
    mocks = "".join(
        f'[[test.mock]]\ncall = "tool:{n}"\nreturns = "x"\n'
        for n in ("check_inventory", "refund_order")
    )
    assert warned(tmp_path, '["tool:*"]', mocks) == []


def test_text_over_64_kib_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"text: is over 64 KiB"):
        with_fault(tmp_path, carried_fault("append", 'text = "' + "x" * 65537 + '"\n'))
    with pytest.raises(ConfigError, match=r"find: is over 64 KiB"):
        with_fault(tmp_path, carried_fault("replace", 'text = "a"\nfind = "' + "x" * 65537 + '"\n'))
    ok = carried_fault("append", 'text = "' + "x" * 65536 + '"\n')
    assert len(with_fault(tmp_path, ok).tests[0].faults[-1].text or "") == 65536


def test_text_and_find_survive_the_plan(tmp_path: Path) -> None:
    cfg = with_fault(tmp_path, carried_fault("replace", 'text = "x"\nfind = "y"\n'))
    sent = plan.plan_for(cfg, plan.rows(cfg)[-1], "run-1").to_json()
    assert sent["faults"] == [
        {"call": "tool:get_order", "do": "replace", "when": "first 1", "text": "x", "find": "y"}
    ]
    back = plan.Plan.from_json(sent).faults[0]
    assert (back.text, back.find) == ("x", "y")


def test_a_tool_listed_by_name_under_real_is_named_as_a_write(tmp_path: Path) -> None:
    [warning] = warned(tmp_path, '["tool:check_inventory", "tool:refund_order"]')
    assert "these would run for real if it did: tool:check_inventory; tool:refund_order." in warning
    # A mocked tool is not named, and tool:* already names the rest.
    mock = '[[test.mock]]\ncall = "tool:check_inventory"\nreturns = "x"\n'
    [again] = warned(tmp_path, '["tool:check_inventory", "tool:refund_order"]', mock)
    assert "did: tool:refund_order." in again
    [star] = warned(tmp_path, '["tool:*", "tool:refund_order"]')
    assert star.count("refund_order") == 1


def test_the_find_is_raw_in_the_plan_and_redacted_in_the_results(tmp_path: Path) -> None:
    from scrimmage_core import runner

    key = "sk-" + "b" * 20
    fault = config.Fault("tool:t", "replace", "always", text="x", find=f"a {key}")
    assert plan.fault_json(fault)["find"] == f"a {key}"  # the plan applies the fault: raw
    shown = runner.fault_json(fault)
    assert shown is not None and shown["find"] == "a [secret removed]"
    plain = config.Fault("tool:t", "replace", "always", text="x", find="late")
    assert plan.fault_json(plain)["find"] == "late"


def test_real_cannot_list_a_model_path_as_a_web_request(tmp_path: Path) -> None:
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        'real = ["http:guard.internal POST /v1/chat/completions"]\n'
        '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["ok"]\n'
    )
    with pytest.raises(ConfigError, match="lists a model call as a web request") as caught:
        load_text(tmp_path, text)
    assert '{host = "guard.internal", provider = "openai"}' in str(caught.value)
    # Another web write, or the host and method alone, is the team's to list.
    for entry in ("POST /v1/refunds", "POST"):
        assert load_text(tmp_path, text.replace("POST /v1/chat/completions", entry))


@pytest.mark.design("config.connection_faults")
@pytest.mark.parametrize("word", ["refused", "reset", "dns"])
def test_connection_faults_work_on_model_and_web_only(tmp_path: Path, word: str) -> None:
    for call in ("model", "model:openai", "model:*/embeddings", "http:inventory.acme.internal"):
        fault = example(tmp_path, FAULT, model_fault(word, "first 1", call)).tests[0].faults[0]
        assert (fault.call, fault.do) == (call, word)
    raises = config.CONNECTION[word]
    for call in ("tool:get_order", "mcp:docs/search"):
        table: dict[str, object] = {"call": call, "do": word, "when": "first 1"}
        with pytest.raises(ConfigError) as caught:
            config.read_fault(table, "[[test.fault]] 1")
        assert f'do = "error" with raises = "{raises}"' in str(caught.value)
    # What the message names loads, as a built-in a tool raises.
    named = model_fault("error", "first 1", "tool:get_order") + f'raises = "{raises}"\n'
    assert example(tmp_path, FAULT, named).tests[0].faults[0].raises == raises
