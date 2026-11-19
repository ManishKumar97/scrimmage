"""A mock built by the team's own function: returns_from, for a value TOML cannot hold."""

import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import jsonschema
import pytest

from conftest import OK, ROW_FAILED, Project
from scrimmage_core import config, state, tool_gate
from scrimmage_core import plan as planner
from scrimmage_core.config import Mock
from scrimmage_core.errors import ConfigError, ScrimmageMockError, ScrimmageRefused
from scrimmage_core.plan import Plan

HERE = "test_returns_from"
PLAN_SCHEMA = Path(config.__file__).parent / "schemas" / "plan.v1.json"

INVOICES = """
from pathlib import Path


def get_invoice(invoice_id: str) -> bytes:
    Path(__file__).parent.parent.joinpath("tool-ran").write_text(invoice_id)  # the real system
    return b"real"


def answer(question: str) -> str:
    pdf = get_invoice("INV-7")
    return f"a PDF of {len(pdf)} bytes" if pdf.startswith(b"%PDF") else "no invoice"
"""
INVOICE_TEST = """[agent]
function     = "acme.invoices:answer"
tools        = ["acme.invoices:get_invoice"]
max_cost_usd = "none"

[[test]]
name     = "invoice"
question = "Send me invoice INV-7"
expect   = ["PDF"]

[[test.mock]]
call         = "tool:get_invoice"
returns_from = "acme.test_mocks:invoice_pdf"
"""


class Lines:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append(line)


@pytest.fixture
def lines() -> Iterator[Lines]:
    yield Lines()
    state.install(None)


def install(lines: Lines, mocks: Sequence[Mock], prefixes: Sequence[str] = (f"{HERE}.",)) -> None:
    p = Plan("run-1", "invoice", "baseline", tuple(mocks), (), (), (), 60)
    state.install(state.RunState(p, lines, list(prefixes)))


def made(factory: Callable[[], object], args: dict[str, object] | None = None) -> Mock:
    path = f"{HERE}:{factory.__name__}"
    return Mock("tool:get_invoice", None, None, args=args, returns_from=path)


def get_invoice(invoice_id: str) -> bytes:
    raise AssertionError(f"the real tool must never run under a mock, and ran for {invoice_id}")


calls: list[bytearray] = []


def counted() -> bytearray:  # a bytearray, so the agent can change it
    value = bytearray(b"%PDF-1.7 call")
    calls.append(value)
    return value


def broken() -> bytes:
    raise ValueError("card 4242-4242-4242-4242 is not on file")


async def coroutine_factory() -> bytes:
    return b"%PDF"


def awaitable_factory() -> object:
    return coroutine_factory()


def pdf() -> bytes:
    return b"%PDF-1.7"


@pytest.mark.slow
@pytest.mark.design("mock.returns_from")
def test_a_bytes_tool_is_mocked_by_its_factory(project: Project) -> None:
    (project.root / "acme" / "invoices.py").write_text(INVOICES)
    project.toml.write_text(INVOICE_TEST)
    cfg = config.load(project.toml)
    for row in planner.rows(cfg):  # the plan carries the path, never code
        doc = planner.plan_for(cfg, row, "run-1").to_json()
        jsonschema.Draft202012Validator(json.loads(PLAN_SCHEMA.read_text())).validate(doc)
    result = project.test()
    assert result.code == OK, result.out + result.err
    calls_made = [line for run in result.records() for line in run if "call" in line]
    assert len(calls_made) == 3 and {line["action"] for line in calls_made} == {"mock"}
    assert not (project.root / "tool-ran").exists()  # the tool never ran
    # Positive control: run for real, the same tool leaves its file, so the check can see a run.
    mock = INVOICE_TEST.index("[[test.mock]]")
    real = INVOICE_TEST[:mock].replace(
        "max_cost_usd", 'real         = ["tool:get_invoice"]\n' + "max_cost_usd"
    )
    project.toml.write_text(real)
    assert project.test().code == ROW_FAILED
    assert (project.root / "tool-ran").read_text() == "INV-7"


@pytest.mark.design("mock.returns_from_every_call")
def test_the_factory_is_called_on_every_call(lines: Lines) -> None:
    calls.clear()
    install(lines, [made(counted)])
    gated = tool_gate.gate(get_invoice, "get_invoice")
    first = gated("INV-7")
    first.extend(b" changed by the agent")
    values = [first, gated("INV-7"), gated("INV-7")]
    assert len(calls) == 3 and values == calls
    assert values[1] == values[2] == b"%PDF-1.7 call"  # the change is not seen by the next call
    assert [line["action"] for line in lines.lines] == ["mock"] * 3


@pytest.mark.design("mock.returns_from_args")
def test_returns_from_with_args_answers_only_those_arguments(lines: Lines) -> None:
    install(lines, [made(pdf, args={"invoice_id": "INV-7"})])
    gated = tool_gate.gate(get_invoice, "get_invoice")
    assert gated("INV-7") == b"%PDF-1.7"
    with pytest.raises(ScrimmageRefused):  # no mock answers, and the tool is not under real
        gated("INV-8")
    assert [line["action"] for line in lines.lines] == ["mock", "refused"]


@pytest.mark.design("mock.returns_from_raises")
def test_a_factory_that_raises_is_a_mistake_naming_it_and_not_its_message(lines: Lines) -> None:
    install(lines, [made(broken)])
    with pytest.raises(ScrimmageMockError) as caught:
        tool_gate.gate(get_invoice, "get_invoice")("INV-7")
    message = f"returns_from {HERE}:broken raised ValueError"
    assert str(caught.value) == message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert lines.lines[0]["mistake"] == message
    leaked = "4242" in json.dumps(lines.lines) or "4242" in str(caught.value)
    assert not leaked, "the factory's error message reached a line"


@pytest.mark.design("mock.returns_from_async")
@pytest.mark.filterwarnings("error::RuntimeWarning")  # a coroutine left unawaited
@pytest.mark.parametrize(
    ("factory", "tail"),
    [(coroutine_factory, ""), (awaitable_factory, ": it returned an awaitable")],
    ids=["async-def", "returns-a-coroutine"],
)
def test_an_async_factory_is_a_mistake(
    lines: Lines, factory: Callable[[], object], tail: str
) -> None:
    install(lines, [made(factory)])
    with pytest.raises(ScrimmageMockError) as caught:
        tool_gate.gate(get_invoice, "get_invoice")("INV-7")
    message = f"returns_from {HERE}:{factory.__name__} must be a plain function, not async{tail}"
    assert str(caught.value) == message and lines.lines[0]["mistake"] == message


@pytest.mark.design("mock.returns_from_packages")
def test_a_factory_outside_the_agents_packages_is_refused_at_load(tmp_path: Path) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text(INVOICE_TEST.replace("acme.test_mocks:", "billing.fixtures:"))
    with pytest.raises(ConfigError) as caught:
        config.load(path)
    assert str(caught.value) == (
        "[[test]] 'invoice': returns_from billing.fixtures:invoice_pdf is outside the agent's "
        "packages (acme.), so it is refused"
    )
    path.write_text(INVOICE_TEST)  # under acme., it loads
    assert config.load(path).tests[0].mocks[0].returns_from == "acme.test_mocks:invoice_pdf"


@pytest.mark.design("imports.returns_from_refused_before_import")
def test_a_factory_outside_the_allowed_imports_is_never_imported(
    lines: Lines, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "imported"
    module = "returns_from_evil"
    (tmp_path / f"{module}.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('yes')\n"
        "def make():\n    return b'%PDF'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    mock = Mock("tool:get_invoice", None, None, returns_from=f"{module}:make")
    install(lines, [mock], prefixes=["acme."])
    with pytest.raises(ScrimmageMockError) as caught:
        tool_gate.gate(get_invoice, "get_invoice")("INV-7")
    assert str(caught.value) == (
        f"returns_from {module}:make is outside SCRIMMAGE_ALLOW_IMPORTS (acme.), so it is not "
        "imported"
    )
    assert not marker.exists() and module not in sys.modules
    # Positive control: under an allowed prefix the same module is imported, and the file made.
    try:
        install(lines, [mock], prefixes=[f"{module}."])
        assert tool_gate.gate(get_invoice, "get_invoice")("INV-7") == b"%PDF"
        assert marker.exists()
    finally:
        sys.modules.pop(module, None)
