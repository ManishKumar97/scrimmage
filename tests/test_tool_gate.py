"""The tools-list gate and run state, in one process, with lines kept in a list."""

import asyncio
import inspect
import sys
import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pytest

from faultline_sdk import state, tool_gate
from faultline_sdk.config import Fault, Mock
from faultline_sdk.errors import (
    FaultlineMockError,
    FaultlineRefused,
    FaultlineTimeLimit,
)
from faultline_sdk.plan import Plan


@dataclass
class Stock:
    sku: str
    count: int


class Validated:
    """Stands in for a pydantic model: built with model_validate."""

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    @classmethod
    def model_validate(cls, data: object) -> "Validated":
        if not isinstance(data, dict) or "sku" not in data:
            raise ValueError("sku is required")
        return cls(dict(data))  # pyright: ignore[reportUnknownArgumentType]


def get_stock(sku: str) -> Stock:
    return Stock(sku, 1)


def get_validated(sku: str) -> Validated:
    return Validated({"sku": sku})


def get_text(order_id: str) -> str:
    return "real"


async def get_async(order_id: str) -> str:
    return "real async"


class Lines:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []
        self.kept: list[bool] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append(line)
        self.kept.append(keep)


@pytest.fixture
def lines() -> Iterator[Lines]:
    yield Lines()
    state.install(None)


def install(
    lines: Lines,
    mocks: Sequence[Mock] = (),
    faults: Sequence[Fault] = (),
    real: Sequence[str] = (),
    limit: float = 60,
) -> state.RunState:
    p = Plan(
        "run-1", "order-status", "fault 1", tuple(mocks), tuple(faults), (), tuple(real), limit
    )
    run = state.RunState(p, lines, ["tests.", "test_tool_gate."])
    state.install(run)
    return run


def test_without_a_run_the_gate_calls_the_tool(lines: Lines) -> None:
    state.install(None)
    assert tool_gate.gate(get_text, "get_text")("1") == "real"
    assert lines.lines == []


@pytest.mark.design("mock.typed_from_annotation")
def test_a_table_becomes_the_tools_own_type(lines: Lines) -> None:
    install(lines, [Mock("tool:get_stock", {"sku": "SKU-42", "count": 12}, None)])
    assert tool_gate.gate(get_stock, "get_stock")("SKU-42") == Stock("SKU-42", 12)
    assert lines.lines[0]["action"] == "mock"


@pytest.mark.design("mock.model_validate")
def test_a_pydantic_style_type_is_built_with_model_validate(lines: Lines) -> None:
    install(lines, [Mock("tool:get_validated", {"sku": "SKU-42"}, None)])
    value = tool_gate.gate(get_validated, "get_validated")("SKU-42")
    assert isinstance(value, Validated) and value.data == {"sku": "SKU-42"}


@pytest.mark.design("mock.misfit_is_a_mistake")
@pytest.mark.parametrize("returns", [{"sku": "SKU-42"}, "not a table"])
def test_a_mock_that_does_not_fit_is_a_mistake(lines: Lines, returns: object) -> None:
    install(lines, [Mock("tool:get_stock", returns, None)])
    with pytest.raises(FaultlineMockError, match="does not fit"):
        tool_gate.gate(get_stock, "get_stock")("SKU-42")
    assert "does not fit" in str(lines.lines[0]["mistake"])


def test_a_validation_error_is_a_mistake(lines: Lines) -> None:
    install(lines, [Mock("tool:get_validated", {"count": 1}, None)])
    with pytest.raises(FaultlineMockError, match="sku is required"):
        tool_gate.gate(get_validated, "get_validated")("SKU-42")


def test_an_explicit_type_wins_over_the_annotation(lines: Lines) -> None:
    install(lines, [Mock("tool:get_text", {"sku": "A", "count": 1}, "test_tool_gate:Stock")])
    assert tool_gate.gate(get_text, "get_text")("1") == Stock("A", 1)


@pytest.mark.design("imports.allowed_prefixes")
def test_a_type_outside_the_allowed_prefixes_is_refused_before_import(
    lines: Lines,
) -> None:
    install(lines, [Mock("tool:get_text", {"a": 1}, "never_imported_module:Thing")])
    with pytest.raises(FaultlineRefused, match="outside FAULTLINE_ALLOW_IMPORTS"):
        tool_gate.gate(get_text, "get_text")("1")
    assert "never_imported_module" not in sys.modules


def test_a_plain_annotation_returns_the_value_as_written(lines: Lines) -> None:
    install(lines, [Mock("tool:get_text", "shipped, Oct 30", None)])
    assert tool_gate.gate(get_text, "get_text")("1") == "shipped, Oct 30"


@pytest.mark.design("fault.timeout_tool")
def test_a_timeout_fault_raises_timeout_error_and_records_it(lines: Lines) -> None:
    install(lines, faults=[Fault("tool:get_text", "timeout", "always")])
    with pytest.raises(TimeoutError, match="get_text timed out"):
        tool_gate.gate(get_text, "get_text")("1")
    line = lines.lines[0]
    assert line["action"] == "fault" and line["fault"] == {
        "do": "timeout",
        "when": "always",
        "n": 1,
    }
    assert line["error"] == "TimeoutError: get_text timed out" and line["result"] is None


@pytest.mark.design("refused.poisons_run")
def test_the_first_refusal_makes_every_later_call_fail(lines: Lines) -> None:
    install(lines, real=["tool:get_text"])
    with pytest.raises(FaultlineRefused, match="neither mocked by this test nor listed"):
        tool_gate.gate(get_stock, "get_stock")("A")
    with pytest.raises(FaultlineRefused, match="already refused tool:get_stock"):
        tool_gate.gate(get_text, "get_text")("1")  # listed under real, but the run is poisoned
    assert [line["action"] for line in lines.lines] == ["refused", "refused"]
    assert lines.kept == [True, True]  # refusals are never dropped


@pytest.mark.design("refused.not_a_library_error")
def test_the_refusal_error_is_faultlines_own() -> None:
    # Every base is Faultline's own or Python's Exception itself, never a library's error.
    bases = FaultlineRefused.__mro__[1:]
    assert all(
        b.__module__ == "faultline_sdk.errors" or b in (Exception, BaseException, object)
        for b in bases
    )
    assert not issubclass(FaultlineRefused, (OSError, TimeoutError, ValueError, RuntimeError))


def test_a_real_tool_runs_and_its_result_is_recorded(lines: Lines) -> None:
    install(lines, real=["tool:*"])
    assert tool_gate.gate(get_text, "get_text")(order_id="sk-proj-" + "a" * 30) == "real"
    line = lines.lines[0]
    assert line["action"] == "real"
    args = line["args"]
    assert isinstance(args, dict) and "sk-proj" not in str(args["preview"])


def test_a_real_tool_error_is_recorded_and_raised(lines: Lines) -> None:
    install(lines, real=["tool:*"])

    def broken(x: int) -> int:
        raise KeyError(x)

    with pytest.raises(KeyError):
        tool_gate.gate(broken, "broken")(1)
    assert lines.lines[0]["error"] == "KeyError: 1"


@pytest.mark.design("gate.same_kind_async")
def test_an_async_tool_gets_an_async_gate(lines: Lines) -> None:
    install(lines, [Mock("tool:get_async", "mocked", None)], real=["tool:get_async"])
    gated = tool_gate.gate(get_async, "get_async")
    assert inspect.iscoroutinefunction(gated)
    assert asyncio.run(gated("1")) == "mocked"
    install(lines, real=["tool:get_async"])
    assert asyncio.run(gated("1")) == "real async"
    install(lines, faults=[Fault("tool:get_async", "timeout", "always")])
    with pytest.raises(TimeoutError):
        asyncio.run(gated("1"))
    state.install(None)
    assert asyncio.run(gated("1")) == "real async"


def test_an_async_real_tool_error_is_recorded(lines: Lines) -> None:
    install(lines, real=["tool:*"])

    async def broken() -> None:
        raise OSError("down")

    with pytest.raises(OSError):
        asyncio.run(tool_gate.gate(broken, "broken")())
    assert lines.lines[0]["error"] == "OSError: down"


@pytest.mark.design("run.time_limit")
def test_a_gate_stops_a_call_once_the_time_limit_has_passed(lines: Lines) -> None:
    run = install(lines, real=["tool:*"], limit=5)
    run.began -= 6
    with pytest.raises(FaultlineTimeLimit, match="time limit of 5s"):
        tool_gate.gate(get_text, "get_text")("1")


@pytest.mark.design("tools.swap_import_path")
def test_the_hook_swaps_tools_when_their_module_is_imported(
    lines: Lines,
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    (folder / "shopkit").mkdir()
    (folder / "shopkit" / "__init__.py").write_text("")
    (folder / "shopkit" / "tools.py").write_text("def ask(q):\n    return 'real'\n")
    monkeypatch.syspath_prepend(str(folder))
    install(lines, [Mock("tool:ask", "mocked", None)])
    before = list(sys.meta_path)
    tool_gate.install(["shopkit.tools:ask"])
    try:
        from shopkit import tools  # type: ignore[import-not-found]

        assert tools.ask("q") == "mocked"
        tool_gate.swap(tools, ["ask"])  # a second swap does not wrap twice
        assert not hasattr(tools.ask.__wrapped__, tool_gate.GATED)
    finally:
        sys.meta_path[:] = before
        for name in ("shopkit", "shopkit.tools"):
            sys.modules.pop(name, None)


def test_a_module_imported_before_the_sdk_is_swapped_at_once(lines: Lines) -> None:
    module = types.ModuleType("early_tools")
    module.ask = get_text  # type: ignore[attr-defined]
    sys.modules["early_tools"] = module
    install(lines, [Mock("tool:ask", "mocked", None)])
    before = list(sys.meta_path)
    try:
        tool_gate.install(["early_tools:ask"])
        assert module.ask("1") == "mocked"  # type: ignore[attr-defined]
    finally:
        sys.meta_path[:] = before
        del sys.modules["early_tools"]


def test_arguments_that_do_not_bind_are_still_recorded(lines: Lines) -> None:
    install(lines, [Mock("tool:get_text", "m", None)])
    assert tool_gate.gate(get_text, "get_text")(1, 2, extra=3) == "m"
    args = lines.lines[0]["args"]
    assert isinstance(args, dict) and '"kwargs"' in str(args["preview"])


@pytest.mark.design("tools.listed_missing_stops")
def test_a_listed_tool_that_does_not_exist_fails_the_import_and_the_run(
    lines: Lines, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    (folder / "shopkit2.py").write_text("def refund_order(o):\n    return 'REAL REFUND'\n")
    monkeypatch.syspath_prepend(str(folder))
    install(lines, real=["tool:*"])
    before = list(sys.meta_path)
    tool_gate.install(["shopkit2:refund_ordr"])
    try:
        with pytest.raises(
            tool_gate.ListedToolMissing, match="shopkit2 has no function refund_ordr"
        ):
            import shopkit2  # type: ignore[import-not-found]  # noqa: F401
        assert "shopkit2" not in sys.modules  # the real refund_order cannot be reached
        assert lines.lines[0]["action"] == "refused" and "refund_ordr" in str(
            lines.lines[0]["mistake"]
        )
    finally:
        sys.meta_path[:] = before
        sys.modules.pop("shopkit2", None)


def test_module_exists_does_not_import(tmp_path: object) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    (folder / "pkg_never").mkdir()
    (folder / "pkg_never" / "__init__.py").write_text("raise SystemExit('imported')\n")
    (folder / "pkg_never" / "tools.py").write_text("")
    assert tool_gate.module_exists("pkg_never.tools", [str(folder)])
    assert not tool_gate.module_exists("pkg_never.tool", [str(folder)])
    assert not tool_gate.module_exists("no_such_pkg.tools", [str(folder)])
    assert "pkg_never" not in sys.modules


@pytest.mark.design("record.redacts_errors")
def test_errors_and_mistakes_are_redacted(lines: Lines) -> None:
    install(lines, real=["tool:*"])

    def leaky() -> None:
        raise ValueError("call failed with key sk-proj-" + "b" * 30)

    with pytest.raises(ValueError):
        tool_gate.gate(leaky, "leaky")()
    assert "sk-proj" not in str(lines.lines[0]["error"]) and "[secret removed]" in str(
        lines.lines[0]["error"]
    )


@pytest.mark.design("boot.fork_no_faults")
def test_a_forked_child_fires_no_faults(lines: Lines) -> None:
    import os

    run = install(
        lines,
        [Mock("tool:get_text", "mocked", None)],
        [Fault("tool:get_text", "timeout", "always")],
    )
    os.register_at_fork(after_in_child=run.drop_faults)
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child
        try:
            result = tool_gate.gate(get_text, "get_text")("1")
        except BaseException as exc:
            result = type(exc).__name__
        os.write(write, str(result).encode())
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    assert os.read(read, 100) == b"mocked"
    with pytest.raises(TimeoutError):  # the parent still fires the fault
        tool_gate.gate(get_text, "get_text")("1")
