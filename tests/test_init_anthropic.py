"""scrimmage init on an agent written with the anthropic library, or with both libraries."""

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from fakes.anthropic_server import FakeAnthropic
from fakes.openai_server import FakeOpenAI
from scrimmage import cli
from scrimmage import init as drafting
from scrimmage.outcomes import OK
from test_init import agent_table, probe_of, run, write

GATEWAY = "https://gateway.acme.internal:8443"
ENTRY = "import anthropic\n\nclient = anthropic.Anthropic(base_url={url!r})\n\n\n"
ANSWER = "def answer(question):\n    return question\n"
BOTH = (
    "import openai\nimport anthropic\n\n"
    "A = openai.OpenAI(base_url={a!r})\nB = anthropic.Anthropic(base_url={b!r})\n\n\n" + ANSWER
)
ONE_TEST = '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'


def entry_why(doc: dict[str, object]) -> str:
    candidates = cast(list[dict[str, str]], cast(dict[str, object], doc["entry"])["candidates"])
    return candidates[0]["why"]


def hosts_of(doc: dict[str, object]) -> list[dict[str, str]]:
    return cast(list[dict[str, str]], doc["model_hosts"])


@pytest.mark.design("init.model_host")
def test_an_anthropic_agent_is_named_and_its_gateway_is_drafted_and_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY.format(url=GATEWAY + "/v1") + ANSWER})
    code, doc = run(capsys, str(tmp_path))
    assert code == OK
    assert "in a module that imports anthropic" in entry_why(doc)
    assert "openai" not in entry_why(doc)
    assert hosts_of(doc) == [
        {"host": "gateway.acme.internal", "provider": "anthropic", "found_by": "static"}
    ]
    toml = tmp_path / "scrimmage.toml"
    assert agent_table(toml)["model_hosts"] == [
        {"host": "gateway.acme.internal", "provider": "anthropic"}
    ]
    toml.write_text(toml.read_text() + ONE_TEST)
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


@pytest.mark.design("init.model_host")
def test_anthropics_own_host_needs_no_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": ENTRY.format(url="https://api.anthropic.com") + ANSWER})
    _, doc = run(capsys, str(tmp_path))
    assert doc["model_hosts"] == []


@pytest.mark.design("init.model_host")
def test_an_agent_with_both_libraries_names_both_and_each_host_has_its_own_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path,
        {"bot.py": BOTH.format(a="https://openai.acme.internal/v1", b=GATEWAY)},
    )
    code, doc = run(capsys, str(tmp_path))
    assert code == OK
    assert "in a module that imports openai and anthropic" in entry_why(doc)
    assert hosts_of(doc) == [
        {"host": "gateway.acme.internal", "provider": "anthropic", "found_by": "static"},
        {"host": "openai.acme.internal", "provider": "openai", "found_by": "static"},
    ]
    toml = tmp_path / "scrimmage.toml"
    toml.write_text(toml.read_text() + ONE_TEST)
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


@pytest.mark.design("init.model_host")
def test_one_gateway_for_both_clients_is_listed_once_for_each_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"bot.py": BOTH.format(a=GATEWAY + "/v1", b=GATEWAY)})
    _, doc = run(capsys, str(tmp_path))
    assert [(h["host"], h["provider"]) for h in hosts_of(doc)] == [
        ("gateway.acme.internal", "anthropic"),
        ("gateway.acme.internal", "openai"),
    ]
    toml = tmp_path / "scrimmage.toml"
    toml.write_text(toml.read_text() + ONE_TEST)
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


@pytest.mark.design("init.model_host")
def test_an_openai_client_at_anthropics_host_is_still_a_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each client's own provider decides what its host is, not every provider's."""
    write(tmp_path, {"bot.py": BOTH.format(a="https://api.anthropic.com/v1", b=GATEWAY)})
    _, doc = run(capsys, str(tmp_path))
    assert [(h["host"], h["provider"]) for h in hosts_of(doc)] == [
        ("api.anthropic.com", "openai"),
        ("gateway.acme.internal", "anthropic"),
    ]
    toml = tmp_path / "scrimmage.toml"
    toml.write_text(toml.read_text() + ONE_TEST)
    capsys.readouterr()
    assert cli.main(["check", str(toml)]) == OK, capsys.readouterr().err


def test_every_library_that_names_a_provider_is_one_the_registry_has() -> None:
    from scrimmage.providers.registry import built_in

    names = {p.name for p in built_in().providers}
    assert set(drafting.MODEL_LIBRARIES) == names
    assert set(drafting.CLIENTS.values()) == names
    assert set(drafting.MODEL_LIBRARIES) <= set(drafting.LIBRARIES)


@pytest.mark.slow
@pytest.mark.design("init.probe_any_host")
def test_the_probe_names_a_host_that_is_not_anthropics_and_nothing_is_sent(
    tmp_path: Path,
    fake_anthropic: Callable[..., FakeAnthropic],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import anthropic\n\nclient = anthropic.Anthropic(max_retries=0)\n\n\n"
        "def answer(question):\n"
        "    client.messages.create(model='claude-haiku-4-5', max_tokens=16,\n"
        "        messages=[{'role': 'user', 'content': question}])\n"
        "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_anthropic([])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.base_url)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "draft.toml"
    exit_code, doc = run(capsys, str(tmp_path), "--probe", "bot:answer", "--out", str(out))
    assert exit_code == OK, doc
    assert fake.received == []
    assert hosts_of(doc) == [{"host": "127.0.0.1", "provider": "anthropic", "found_by": "probe"}]
    assert "passed 1 request with no tools" in str(probe_of(doc)["outcome"])
    assert agent_table(out)["model_hosts"] == [{"host": "127.0.0.1", "provider": "anthropic"}]
    out.write_text(out.read_text() + ONE_TEST)
    capsys.readouterr()
    assert cli.main(["check", str(out)]) == OK, capsys.readouterr().err


@pytest.mark.slow
@pytest.mark.design("init.probe_any_host")
def test_an_agent_that_picks_its_client_by_key_is_probed_on_the_key_it_has(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placeholder is set only when the agent has no key, so one it has is not outvoted."""
    code = (
        "import os\nimport anthropic\nimport openai\n\n\n"
        "def answer(question):\n"
        "    if os.environ.get('ANTHROPIC_API_KEY'):\n"
        "        anthropic.Anthropic(max_retries=0).messages.create(model='m', max_tokens=16,\n"
        "            messages=[{'role': 'user', 'content': question}])\n"
        "    else:\n"
        "        openai.OpenAI(max_retries=0).chat.completions.create(model='m',\n"
        "            messages=[{'role': 'user', 'content': question}])\n"
        "    return 'done'\n"
    )
    write(tmp_path, {"bot.py": code})
    fake = fake_openai([])
    monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-looking-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exit_code, doc = run(
        capsys, str(tmp_path), "--probe", "bot:answer", "--out", str(tmp_path / "d.toml")
    )
    assert exit_code == OK, doc
    assert fake.received == []
    assert hosts_of(doc) == [{"host": "127.0.0.1", "provider": "openai", "found_by": "probe"}]


@pytest.mark.parametrize(
    ("provider", "lines"),
    [("openai", [("api.anthropic.com", "openai")]), ("anthropic", [])],
    ids=["another-providers-host", "its-own-host"],
)
def test_a_probed_host_is_a_line_unless_it_is_the_requests_own_providers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    lines: list[tuple[str, str]],
) -> None:
    """The probe's request names its provider, and only that provider's own hosts need no line."""
    request = {
        "host": "api.anthropic.com",
        "method": "POST",
        "path": "/v1/chat/completions",
        "provider": provider,
    }
    notice = cast(dict[str, object], {"tools": [], "model_request": request})
    outcome = "stopped at POST /v1/chat/completions on api.anthropic.com"
    monkeypatch.setattr(drafting, "run_probe", lambda *a: drafting.Probe(True, outcome, notice))
    write(tmp_path, {"bot.py": ANSWER})
    _, doc = run(capsys, str(tmp_path), "--probe", "bot:answer")
    assert [(h["host"], h["provider"]) for h in hosts_of(doc)] == lines


@pytest.mark.design("init.model_host")
@pytest.mark.parametrize(
    ("client", "import_line", "library", "gateway_hosts"),
    [
        ("Anthropic", "import anthropic", "anthropic", ["gateway.acme.internal"]),
        ("AsyncAnthropic", "import anthropic", "anthropic", ["gateway.acme.internal"]),
        # Read through langchain, whose package this is a part of; its base_url is not read.
        ("ChatAnthropic", "import langchain_anthropic", "langchain", []),
    ],
)
def test_each_anthropic_client_class_is_named_and_a_missing_base_url_is_no_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    client: str,
    import_line: str,
    library: str,
    gateway_hosts: list[str],
) -> None:
    module = import_line.removeprefix("import ")
    plain = f"{import_line}\nC = {module}.{client}()\n\n\n" + ANSWER
    write(tmp_path, {"bot.py": plain})
    _, doc = run(capsys, str(tmp_path))
    assert f"in a module that imports {library} and builds a model client" in entry_why(doc)
    assert doc["model_hosts"] == []
    with_url = f"{import_line}\nC = {module}.{client}(base_url={GATEWAY!r})\n\n\n" + ANSWER
    write(tmp_path, {"bot.py": with_url})
    _, doc = run(capsys, str(tmp_path), "--out", str(tmp_path / "again.toml"))
    assert [(h["host"], h["provider"]) for h in hosts_of(doc)] == [
        (host, "anthropic") for host in gateway_hosts
    ]
