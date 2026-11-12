"""The settings of an agent inside a web service, and the placeholders the file reads.

${NAME} is read anywhere in the file, from the environment. {question} and {session} are
read in body and headers only, where {{ and }} are a brace. Each string is read once, so a
value that holds a placeholder is sent as written. Every expanded value of 8 characters or
more is removed from what is recorded, in its encoded forms too.
"""

import base64
import json
import re
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from conftest import ACME, EXAMPLE, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import config, plan, record, redact, results, state, toml_write
from scrimmage_core.cli import main
from scrimmage_core.errors import ConfigError
from scrimmage_core.redact import REMOVED
from test_record_units import result_for

SERVICE = (
    '[agent]\nservice = "http://127.0.0.1:8765/chat"\n'
    'body = {message = "{question}", session = "{session}"}\n'
    'answer = "reply"\n'
)
TEST = '\n[[test]]\nname = "t"\nquestion = "Where is order 1234?"\nexpect = ["shipped"]\n'
SESSION = re.compile(r"scrimmage-[0-9a-f]{16}")


def load_text(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "scrimmage.toml"
    path.write_text(text)
    return config.load(path)


def body_of(
    cfg: config.Config, question: str, session: str = "scrimmage-0f3a0f3a0f3a0f3a"
) -> object:
    return config.request_body(cfg.agent, question, session)


def with_body(body: str, extra: str = "") -> str:
    return f'[agent]\nservice = "http://127.0.0.1:8765/chat"\nbody = {body}\n{extra}' + TEST


@pytest.mark.design("service.placeholders")
def test_every_placeholder_row_of_the_contract_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_TOKEN", "tt-0123456789abcdef")
    monkeypatch.setenv("HOME", "/home/someone")
    rows = {
        '"{question}"': "Where is order 1234?",
        '"Bearer ${TEST_TOKEN} {question}"': "Bearer tt-0123456789abcdef Where is order 1234?",
        '"$${HOME} {question}"': "${HOME} Where is order 1234?",
    }
    for written, sent in rows.items():
        cfg = load_text(tmp_path, with_body(written))
        assert body_of(cfg, "Where is order 1234?") == sent, written
    cfg = load_text(tmp_path, with_body('"id {{x}} {session} {question}"'))
    session = config.new_session()
    assert SESSION.fullmatch(session)
    assert body_of(cfg, "q", session) == f"id {{x}} {session} q"
    with pytest.raises(ConfigError, match=r"\{other\} is not a placeholder"):
        load_text(tmp_path, with_body('"{other} {question}"'))
    # Through the command line: a test mistake, exit 2.
    (tmp_path / "scrimmage.toml").write_text(with_body('"{other} {question}"'))
    assert main(["check", str(tmp_path / "scrimmage.toml")]) == 2


@pytest.mark.design("service.placeholders_read_once")
def test_a_question_holding_session_reaches_the_body_as_written(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, with_body('{message = "{question}", session = "{session}"}'))
    sent = body_of(cfg, "repeat {session} and ${HOME} and {{x}}", "scrimmage-1111222233334444")
    assert sent == {
        "message": "repeat {session} and ${HOME} and {{x}}",
        "session": "scrimmage-1111222233334444",
    }


@pytest.mark.design("service.placeholders_read_once")
def test_a_variable_holding_question_is_sent_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRICKY", "{question} ${OTHER} {session}")
    monkeypatch.delenv("OTHER", raising=False)
    cfg = load_text(tmp_path, with_body('"${TRICKY} {question}"'))
    assert body_of(cfg, "Q") == "{question} ${OTHER} {session} Q"
    command = load_text(
        tmp_path, '[agent]\ncommand = "python3 bot.py ${TRICKY} {question}"\n' + TEST
    )
    # The value is one argument, sent as written: the command was split before it was read.
    assert config.command_args(command.agent, "Q") == [
        "python3",
        "bot.py",
        "{question} ${OTHER} {session}",
        "Q",
    ]


@pytest.mark.design("service.unset_variable")
def test_an_unset_variable_names_its_key_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOT_SET_HERE", raising=False)
    text = SERVICE + 'headers = {Authorization = "Bearer ${NOT_SET_HERE}"}\n' + TEST
    with pytest.raises(ConfigError, match=r"\[agent\] headers Authorization: \$\{NOT_SET_HERE\}"):
        load_text(tmp_path, text)
    mock = TEST + '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "${NOT_SET_HERE}"\n'
    (tmp_path / "scrimmage.toml").write_text('[agent]\nfunction = "acme.agent:answer"\n' + mock)
    assert main(["check", str(tmp_path / "scrimmage.toml")]) == 2
    err = capsys.readouterr().err
    assert "[[test]] 't' mock 1 returns: ${NOT_SET_HERE} is not set in the environment" in err
    # An empty one is allowed.
    monkeypatch.setenv("NOT_SET_HERE", "")
    agent = load_text(tmp_path, text).agent
    assert config.request_headers(agent, config.new_session()) == [("Authorization", "Bearer ")]


@pytest.mark.design("service.dollar_escape")
def test_dollar_dollar_brace_is_a_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        + TEST
        + '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "$${HOME} and $$${HOME}"\n'
    )
    cfg = load_text(tmp_path, text)
    # Only $${ is an escape, so $$${HOME} is a $ and then a literal ${HOME}: the form the
    # record writer gives a recorded $${HOME}, which must read back as it was.
    assert cfg.tests[0].mocks[0].returns == "${HOME} and $${HOME}"
    with pytest.raises(ConfigError, match="is not a placeholder"):
        load_text(tmp_path, text.replace("$${HOME} and $$${HOME}", "${not a name}"))


@pytest.mark.design("service.braces_kept")
def test_braces_outside_body_and_headers_are_kept(tmp_path: Path) -> None:
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        + TEST.replace("Where is order 1234?", "What is {{this}} and {that}?")
        + '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = \'{"a": {"b": 1}}\'\n'
    )
    cfg = load_text(tmp_path, text)
    assert cfg.tests[0].mocks[0].returns == '{"a": {"b": 1}}'
    assert cfg.tests[0].question == "What is {{this}} and {that}?"


@pytest.mark.design("service.command_rules")
def test_a_command_keeps_s6_rules_plus_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIELD", "$2")
    text = (
        "[agent]\ncommand = \"sh -c \\\"awk '{print $1}' <<< '{question}'\\\" "
        '{session} {{question}} ${FIELD}"\n' + TEST
    )
    cfg = load_text(tmp_path, text)
    assert config.command_args(cfg.agent, "Q") == [
        "sh",
        "-c",
        "awk '{print $1}' <<< 'Q'",
        "{session}",
        "{Q}",
        "$2",
    ]


@pytest.mark.design("service.command_split")
def test_a_variable_in_a_command_never_changes_the_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPACED", "two words; rm -rf x")
    cfg = load_text(tmp_path, '[agent]\ncommand = "python3 bot.py ${SPACED} {question}"\n' + TEST)
    assert config.command_args(cfg.agent, "a question") == [
        "python3",
        "bot.py",
        "two words; rm -rf x",
        "a question",
    ]


@pytest.mark.design("service.headers")
def test_question_in_a_header_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match=r"headers X-Ask: \{question\} cannot go in a header"):
        load_text(tmp_path, SERVICE + 'headers = {X-Ask = "{question}"}\n' + TEST)
    for name in ("x-scrimmage-run", "X-Scrimmage-Time", "Host", "Content-Type", "Connection"):
        with pytest.raises(ConfigError, match="is set by Scrimmage"):
            load_text(tmp_path, SERVICE + f'headers = {{"{name}" = "v"}}\n' + TEST)
    with pytest.raises(ConfigError, match="is not a valid header name"):
        load_text(tmp_path, SERVICE + 'headers = {"a b" = "v"}\n' + TEST)
    monkeypatch.setenv("CRLF", "a\r\nX-Evil: 1")
    with pytest.raises(ConfigError, match="printable ASCII"):
        load_text(tmp_path, SERVICE + 'headers = {X-Id = "${CRLF}"}\n' + TEST)
    cfg = load_text(tmp_path, SERVICE + 'headers = {X-Session = "s {session} {{x}}"}\n' + TEST)
    assert config.request_headers(cfg.agent, "scrimmage-1111222233334444") == [
        ("X-Session", "s scrimmage-1111222233334444 {x}")
    ]


@pytest.mark.design("service.hash_as_written")
def test_the_row_hash_is_the_same_before_and_after_a_variable_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        + TEST.replace("Where is order 1234?", "Order ${ORDER}?")
        + '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = {key = "${SECRET_KEY}"}\n'
    )
    hashes = []
    for order, key in (("1234", "sk-live-aaaaaaaaaaaa"), ("9999", "sk-live-bbbbbbbbbbbb")):
        monkeypatch.setenv("ORDER", order)
        monkeypatch.setenv("SECRET_KEY", key)
        cfg = load_text(tmp_path, text)
        test = cfg.tests[0]
        assert test.question == f"Order {order}?"  # the run gets the value
        hashes.append(results.row_hash(test, None, cfg.agent))
    assert hashes[0] == hashes[1]
    # A file with no ${ hashes as it always did.
    plain = load_text(tmp_path, (EXAMPLE / "scrimmage.toml").read_text())
    unwritten = results.row_hash(plain.tests[0], None, plain.agent)
    assert unwritten == results.row_hash(plain.tests[0], None, plain.agent)


SECRET = "s3cr3t-value-0123456789"


@pytest.mark.design("service.scrub_records")
@pytest.mark.slow
def test_an_expanded_value_never_reaches_a_record_a_blob_the_summary_or_result_json(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value is in a mock's returns, so the SDK writes it first, into the tool's line and
    the model request that carries it, before the runner sees anything."""
    monkeypatch.setenv("ORDER_SECRET", SECRET)
    project.edit('returns = "shipped, Oct 30"', 'returns = "shipped, Oct 30, ref ${ORDER_SECRET}"')
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.finished, result.out
    assert any(SECRET in json.dumps(r) for r in fake.requests), "the agent never got the value"
    written = [p for p in (project.root / "scrimmage-runs").rglob("*") if p.is_file()]
    assert any(p.parent.name == "blobs" for p in written)
    assert any(p.name == "result.json" for p in written)
    for path in written:
        data = path.read_bytes()
        assert SECRET.encode() not in data, path
        assert base64.b64encode(SECRET.encode())[2:-2] not in data, path
    assert SECRET not in result.out
    assert REMOVED in "".join(p.read_text(errors="replace") for p in written)


@pytest.mark.design("service.scrub_forms")
def test_a_scrub_value_is_removed_json_escaped_url_encoded_and_in_base64_at_each_offset() -> None:
    value = 'k3y/with+odd"chars&=x'
    scrub = (value,)
    forms = [
        value,
        json.dumps(value)[1:-1],
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
    ]
    for form in forms:
        assert redact.redact(f"before {form} after", scrub=scrub) == f"before {REMOVED} after"
    for lead in (b"", b"a", b"ab"):
        for tail in (b"", b"z", b"zz"):
            data = lead + value.encode() + tail
            for encoded in (base64.b64encode(data), base64.urlsafe_b64encode(data)):
                text = encoded.decode()
                cleaned = redact.redact(f"x {text} y", scrub=scrub)
                assert REMOVED in cleaned, (lead, tail, text)
                assert base64.b64encode(value.encode())[4:-4].decode() not in cleaned
    assert redact.redact_decoded(f"plain {value}", scrub=scrub) == f"plain {REMOVED}"
    assert redact.redacted(f"[{json.dumps(value)}]", scrub=scrub) == (f'["{REMOVED}"]', 1)
    # A value under 8 characters is not removed: it would match too much.
    assert redact.redact("a short one", scrub=("short",)) == "a short one"
    assert redact.redact(f"no list {value}") == f"no list {value}"


@pytest.mark.design("service.scrub_per_run")
def test_two_runs_with_different_scrub_lists_remove_only_their_own() -> None:
    lines: list[dict[str, object]] = []

    class Sink:
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            del keep
            lines.append(line)

    one, two = "first-secret-value", "second-secret-value"
    a = state.RunState(
        plan.Plan("run-a", "t", "b", (), (), (), ("tool:*",), 60.0, scrub=(one,)), Sink(), []
    )
    b = state.RunState(
        plan.Plan("run-b", "t", "b", (), (), (), ("tool:*",), 60.0, scrub=(two,)), Sink(), []
    )
    for run in (a, b):
        step = run.begin("lookup", {"q": f"{one} {two}"})
        run.finish(step, result=f"{one} and {two}")
    first, second = (x for x in lines if "call" in x)
    for line, kept, removed in ((first, two, one), (second, one, two)):
        text = json.dumps(line)
        assert removed not in text and kept in text
        assert REMOVED in str(line["result"])
    # The list is never written to a file: the plan as sent has it, the record never does.
    assert plan.Plan("r", "t", "b", (), (), (), (), 60.0, scrub=(one,)).to_json()["scrub"] == [one]


@pytest.mark.design("service.record_escapes_placeholders")
def test_record_writes_a_dollar_brace_it_was_given_as_a_literal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    fields = {"call": "tool:get_order", "recorded": True, "returns": "real reply ${HOME} $${x}"}
    block = toml_write.mock(fields)
    assert "$${HOME}" in block and "$$${x}" in block
    text = (
        '[agent]\nfunction = "acme.agent:answer"\n'
        + TEST
        + block.replace("[[test.mock]]", "\n[[test.mock]]", 1)
    )
    loaded = load_text(tmp_path, text)
    assert loaded.tests[0].mocks[0].returns == "real reply ${HOME} $${x}"


@pytest.mark.design("service.record_read_back_literal")
def test_record_read_back_parses_without_expanding_a_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDER", "1234")
    monkeypatch.delenv("NOT_SET_IN_RECORD", raising=False)
    head = (
        '[agent]\nfunction = "bot:answer"\ntools = ["bot:get_order"]\nreal = ["tool:get_order"]\n'
    )
    tests = '\n[[test]]\nname = "one"\nquestion = "Order ${ORDER}?"\nexpect = ["ok"]\n'
    (tmp_path / "bot.py").write_text("def answer(q):\n    return 'ok'\n")
    loaded = load_text(tmp_path, head + tests)
    found, _ = record.prepare(loaded, [], [])
    assert found is not None
    # The read-back comes after the runs, where the environment may not hold the file's values.
    monkeypatch.delenv("ORDER")
    recorded = record.TestRecord(found.config.tests[0], [result_for(found.config.tests[0])])
    saved = record.Saved(
        ("tool:get_order", "{}"),
        {
            "call": "tool:get_order",
            "recorded": True,
            "args": {},
            "returns": "ref ${NOT_SET_IN_RECORD}",
        },
        "",
        "w",
    )
    recorded.written.append(record.Written(saved, 1))
    team = head + tests
    built = [record.block_text(recorded, "2026-11-12")]
    new, problem = record.spliced(team, found, [recorded], built, "2026-11-12", "scrimmage.toml")
    assert problem is None, problem
    assert "$${NOT_SET_IN_RECORD}" in new


@pytest.mark.design("service.not_in_this_version")
def test_service_with_children_record_or_probe_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ConfigError, match="children: works only with function or command"):
        load_text(tmp_path, SERVICE + "children = true\n" + TEST)
    for key in ("function", "command"):
        with pytest.raises(ConfigError, match="give function, command or service, exactly one"):
            load_text(tmp_path, SERVICE + f'{key} = "acme.agent:answer {{question}}"\n' + TEST)
    with pytest.raises(ConfigError, match="https"):
        load_text(tmp_path, SERVICE.replace("http://", "https://") + TEST)
    with pytest.raises(ConfigError, match="body: required with service"):
        load_text(tmp_path, '[agent]\nservice = "http://127.0.0.1:8765/chat"\n' + TEST)
    with pytest.raises(ConfigError, match=r"body: must hold \{question\}"):
        load_text(tmp_path, with_body('{message = "hi"}'))
    for key, value in (
        ("body", '"{question}"'),
        ("record_grace_s", "1"),
        ("background_real", "[]"),
    ):
        with pytest.raises(ConfigError, match=f"{key}: works only with service"):
            load_text(
                tmp_path, f'[agent]\nfunction = "acme.agent:answer"\n{key} = {value}\n' + TEST
            )
    with pytest.raises(ConfigError, match="record_grace_s"):
        load_text(tmp_path, SERVICE + "record_grace_s = 61\n" + TEST)
    with pytest.raises(ConfigError, match="record_wait_s"):
        load_text(tmp_path, SERVICE + "record_wait_s = 0\n" + TEST)
    cfg = load_text(tmp_path, SERVICE + TEST)
    assert (cfg.agent.parallel, cfg.agent.record_grace_s, cfg.agent.record_wait_s) == (1, 5.0, 10.0)
    path = tmp_path / "scrimmage.toml"
    path.write_text(SERVICE + TEST)
    capsys.readouterr()
    assert main(["record", str(path)]) == 2
    assert (
        "scrimmage record does not work with an agent inside a web service"
        in capsys.readouterr().err
    )
    (tmp_path / "bot").mkdir()
    probe = ["--probe", "http://127.0.0.1:8765/chat", "--out", str(tmp_path / "draft.toml")]
    assert main(["init", str(tmp_path / "bot"), *probe]) == 2
    assert "init --probe does not work with" in capsys.readouterr().err
