"""The tools-list gate and run state, in one process, with lines kept in a list."""

import asyncio
import inspect
import json
import sys
import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

from report_cases import AGENT, TEST, load
from report_cases import run as a_run
from scrimmage_core import decide as engine
from scrimmage_core import plan as planner
from scrimmage_core import state, tool_gate
from scrimmage_core.config import Fault, Mock
from scrimmage_core.errors import (
    ScrimmageMockError,
    ScrimmageRefused,
    ScrimmageTimeLimit,
)
from scrimmage_core.plan import Plan
from scrimmage_core.redact import redact
from scrimmage_core.runner import miss_groups


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


def get_more(order_id: str) -> str:
    return "real"


async def get_async(order_id: str) -> str:
    return "real async"


class Refusal(Exception):
    """A team's own error type."""


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
    with pytest.raises(ScrimmageMockError, match="does not fit"):
        tool_gate.gate(get_stock, "get_stock")("SKU-42")
    assert "does not fit" in str(lines.lines[0]["mistake"])


def test_a_validation_error_is_a_mistake(lines: Lines) -> None:
    install(lines, [Mock("tool:get_validated", {"count": 1}, None)])
    with pytest.raises(ScrimmageMockError, match="sku is required"):
        tool_gate.gate(get_validated, "get_validated")("SKU-42")


def test_an_explicit_type_wins_over_the_annotation(lines: Lines) -> None:
    install(lines, [Mock("tool:get_text", {"sku": "A", "count": 1}, "test_tool_gate:Stock")])
    assert tool_gate.gate(get_text, "get_text")("1") == Stock("A", 1)


@pytest.mark.design("imports.allowed_prefixes")
def test_a_type_outside_the_allowed_prefixes_is_refused_before_import(
    lines: Lines,
) -> None:
    install(lines, [Mock("tool:get_text", {"a": 1}, "never_imported_module:Thing")])
    with pytest.raises(ScrimmageRefused, match="outside SCRIMMAGE_ALLOW_IMPORTS"):
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


@pytest.mark.design("fault.error_tool")
def test_an_error_fault_in_a_hand_written_loop_is_a_plain_error(lines: Lines) -> None:
    install(lines, faults=[Fault("tool:get_text", "error", "always")])
    with pytest.raises(RuntimeError, match="get_text failed"):
        tool_gate.gate(get_text, "get_text")("1")


@pytest.mark.parametrize(
    ("raises", "kind"),
    [("ConnectionError", ConnectionError), ("test_tool_gate:Refusal", Refusal)],
)
def test_an_error_fault_can_name_its_type(lines: Lines, raises: str, kind: type) -> None:
    install(lines, faults=[Fault("tool:get_text", "error", "always", raises)])
    with pytest.raises(kind, match="get_text failed"):
        tool_gate.gate(get_text, "get_text")("1")


class NotAnError:
    pass


class TwoArguments(Exception):
    def __init__(self, first: str, second: str) -> None:
        super().__init__(first, second)


def a_function() -> None:
    pass


@pytest.mark.design("fault.bad_raises_is_a_mistake")
@pytest.mark.parametrize(
    "raises",
    [
        "test_tool_gate:NotAnError",
        "test_tool_gate:TwoArguments",
        "test_tool_gate:a_function",
        "test_tool_gate:Missing",
        "no_such_module_here:Thing",
    ],
)
def test_a_fault_that_cannot_be_raised_is_a_mistake(lines: Lines, raises: str) -> None:
    install(lines, faults=[Fault("tool:get_text", "error", "always", raises)])
    state.current().allowed.append("no_such_module_here.")  # type: ignore[union-attr]
    with pytest.raises(ScrimmageMockError, match="the fault cannot raise"):
        tool_gate.gate(get_text, "get_text")("1")
    assert "the fault cannot raise" in str(lines.lines[0]["mistake"])


@pytest.mark.design("refused.poisons_run")
def test_the_first_refusal_makes_every_later_call_fail(lines: Lines) -> None:
    install(lines, real=["tool:get_text"])
    with pytest.raises(ScrimmageRefused, match="neither mocked by this test nor listed"):
        tool_gate.gate(get_stock, "get_stock")("A")
    with pytest.raises(ScrimmageRefused, match="already refused tool:get_stock"):
        tool_gate.gate(get_text, "get_text")("1")  # listed under real, but the run is poisoned
    assert [line["action"] for line in lines.lines] == ["refused", "refused"]
    assert lines.kept == [True, True]  # refusals are never dropped


@pytest.mark.design("refused.not_a_library_error")
def test_the_refusal_error_is_scrimmages_own() -> None:
    # Every base is Scrimmage's own or Python's Exception itself, never a library's error.
    bases = ScrimmageRefused.__mro__[1:]
    assert all(
        b.__module__ == "scrimmage_core.errors" or b in (Exception, BaseException, object)
        for b in bases
    )
    assert not issubclass(ScrimmageRefused, (OSError, TimeoutError, ValueError, RuntimeError))


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
    with pytest.raises(ScrimmageTimeLimit, match="time limit of 5s"):
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


def test_a_gate_builds_its_tools_signature_once(
    lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(lines, [Mock("tool:get_text", "m", None), Mock("tool:get_more", "m", None)])
    built: list[object] = []
    real = inspect.signature

    def counting(tool: object, **kwargs: object) -> inspect.Signature:
        built.append(tool)
        return real(tool, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(inspect, "signature", counting)
    one = tool_gate.gate(get_text, "get_text")
    for _ in range(100):
        assert one(order_id="1") == "m"
    assert len(built) == 1
    assert lines.lines[-1]["args"] is not None
    other = tool_gate.gate(get_more, "get_more")
    for _ in range(3):
        other("1")
    assert built == [get_text, get_more]


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
    assert tool_gate.module_spec("pkg_never.tools", [str(folder)]) is not None
    assert tool_gate.module_spec("pkg_never.tool", [str(folder)]) is None
    assert tool_gate.module_spec("no_such_pkg.tools", [str(folder)]) is None
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
@pytest.mark.slow
def test_a_forked_child_fires_no_faults(lines: Lines) -> None:
    import os

    run = install(
        lines,
        [Mock("tool:get_text", "mocked", None)],
        [Fault("tool:get_text", "timeout", "always")],
    )
    os.register_at_fork(after_in_child=run.forked)
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


# Evidence for a call that had a mock on its label and matched none.


def lookup(order_id: str, warehouse: str = "") -> str:
    del order_id, warehouse
    return "real"


def login(password: str) -> str:
    return "real"


def search(q: str) -> str:
    return "real"


def args_mock(label: str, args: dict[str, object]) -> Mock:
    return Mock(label, "mocked", None, args=args)


def a_miss(lines: Lines) -> dict[str, object]:
    found = [line for line in lines.lines if "mock_miss" in line]
    assert len(found) == 1, lines.lines
    return cast(dict[str, object], found[0]["mock_miss"])


def pinned(lines: Lines, args: dict[str, object], **kw: object) -> state.RunState:
    return install(lines, [args_mock("tool:lookup", args)], real=["tool:lookup"], **kw)  # type: ignore[arg-type]


@pytest.mark.design("decide.miss_evidence")
def test_a_failure_in_missed_does_not_fail_the_call(
    lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned(lines, {"order_id": "1234"})

    def broken(*_: object) -> None:
        raise RuntimeError("a bug in the evidence code")

    monkeypatch.setattr(state, "missed", broken)
    assert tool_gate.gate(lookup, "lookup")("5678") == "real"
    miss = a_miss(lines)
    assert miss["reason"] == "unreadable" and miss["nearest"] is None and miss["seen"] is None
    assert lines.lines[0]["action"] == "real"


KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
SECRETS = [
    pytest.param("lookup", "q", KEY, id="pattern"),
    pytest.param(
        "lookup",
        "q",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        id="jwt",
    ),
    pytest.param("lookup", "q", "https://x.acme/p?token=abcSECRET123", id="url_query"),
    pytest.param("lookup", "q", '{"password": "hunter2hunter2"}', id="json_text"),
    pytest.param("lookup", "q", "a" * 170 + KEY, id="across_cut"),
    pytest.param("login", "password", "hunter2-plain-text", id="key_name"),
]
HIDDEN = ["abcdefghijklmnopqrstuvwxyz", "dBjftJeZ4", "abcSECRET123", "hunter2", "sk-"]


@pytest.mark.design("decide.miss_evidence")
@pytest.mark.parametrize(("tool", "key", "secret"), SECRETS)
def test_mock_miss_is_cleaned_by_both_rules(lines: Lines, tool: str, key: str, secret: str) -> None:
    fn = {"lookup": lookup, "login": login}[tool]
    first = "order_id" if tool == "lookup" else "password"
    # 1. the call's own value, in a key that differs
    install(lines, [args_mock(f"tool:{tool}", {first: "other"})], real=[f"tool:{tool}"])
    tool_gate.gate(fn, tool)(secret)
    miss = a_miss(lines)
    assert miss["seen"] is not None and len(str(miss["seen"])) <= 200
    # the report and --json are built from the line, so they show what it holds
    shown = json.dumps(miss) + " " + " ".join(group_text(lines))
    for word in HIDDEN:
        assert word not in shown, (word, shown)
    # 2. the nearest mock's value, written by hand
    again = Lines()
    install(again, [args_mock(f"tool:{tool}", {first: secret})], real=[f"tool:{tool}"])
    tool_gate.gate(fn, tool)("clean")
    shown = json.dumps(a_miss(again)) + " " + " ".join(group_text(again))
    for word in HIDDEN:
        assert word not in shown, (word, shown)
    # 3. a differing key's name and a web path hold a pattern
    if redact(secret) == secret:
        return
    call = engine.Call("tool", f"tool:{tool}", tool, args={secret: "a", "b": "c"})
    held = Plan("r", "t", "row", (args_mock(f"tool:{tool}", {"b": "c"}),), (), (), (), 60.0)
    found = state.miss_evidence(call, held, engine.missed(call, held, engine.Decision("real")))
    web = engine.Call("http", "http:x.acme", f"/p/{secret}", method="GET")
    mocks = (Mock("http:x.acme", None, None, "GET", "/stock/1"),)
    q = Plan("run-1", "t", "row", mocks, (), (), (), 60.0)
    path = state.miss_evidence(web, q, engine.missed(web, q, engine.Decision("real")))
    for word in HIDDEN:
        assert word not in json.dumps(found) + json.dumps(path), (word, found, path)


def group_text(lines: Lines) -> list[str]:
    """What the text report and --json would print for these lines."""
    result = a_run(planner.rows(load(AGENT + TEST))[0], 0)
    result.misses = [
        {
            "call": str(x["call"]),
            "action": str(x["action"]),
            **cast(dict[str, object], x["mock_miss"]),
        }
        for x in lines.lines
        if "mock_miss" in x
    ]
    return [text + json.dumps(doc) for doc, text in miss_groups([result])]


@pytest.mark.design("decide.miss_evidence")
def test_only_differing_keys_show_values(lines: Lines) -> None:
    pinned(lines, {"order_id": "1234", "warehouse": "north"})
    tool_gate.gate(lookup, "lookup")("5678", "north")
    miss = a_miss(lines)
    nearest = cast(dict[str, Any], miss["nearest"])
    assert miss["seen"] == '{"order_id": "5678", "warehouse": <str>}'
    assert nearest["args"] == '{"order_id": "1234", "warehouse": <str>}'
    assert nearest["differs"] == ["order_id"] and nearest["mock"] == 0
    assert (miss["reason"], miss["method"], miss["path"]) == ("arguments", None, None)


@pytest.mark.design("decide.miss_evidence")
def test_no_mock_shows_names_and_types_only(lines: Lines) -> None:
    pinned(lines, {"order_id": "1234"})
    with pytest.raises(ScrimmageRefused):
        tool_gate.gate(search, "search")("private free text")
    miss = a_miss(lines)
    assert (miss["reason"], miss["seen"], miss["nearest"]) == ("no_mock", '{"q": <str>}', None)
    assert lines.lines[0]["action"] == "refused" and lines.kept[0] is True


@pytest.mark.design("decide.miss_evidence")
def test_a_miss_leaves_the_args_preview_and_hash_as_they_are(lines: Lines) -> None:
    install(lines, [args_mock("tool:login", {"password": "x"})], real=["tool:login"])
    tool_gate.gate(login, "login")("hunter2-plain-text")
    line = lines.lines[0]
    assert line["args"] == state.content({"password": "hunter2-plain-text"})
    assert "hunter2-plain-text" in str(cast(dict[str, object], line["args"])["preview"])


@pytest.mark.design("decide.miss_evidence")
def test_a_miss_line_stays_under_its_size_cap(lines: Lines) -> None:
    big = {f"key{i}-" + "k" * 100: "v" * 100 for i in range(1000)}
    p = Plan("run-1", "t", "row", (args_mock("tool:big", {"z": 1}),), (), (), ("tool:big",), 60.0)
    run = state.RunState(p, lines, [])
    call = engine.Call("tool", "tool:big", "big", args=big)
    run.finish(run.decide(call, {}))
    miss = a_miss(lines)
    assert len(json.dumps(miss)) <= state.MISS_CAP
    nearest = cast(dict[str, Any], miss["nearest"])
    synthetic: dict[str, object] = {
        "reason": "arguments",
        "seen": "s" * 900,
        "method": None,
        "path": "p" * 900,
        "nearest": {
            "mock": 0,
            "args": "a" * 900,
            "method": None,
            "path": "q" * 900,
            "differs": ["k" * 64] * 20,
            "differs_left_out": 0,
        },
    }
    fitted = state.fit_miss(synthetic)
    assert len(json.dumps(fitted)) <= state.MISS_CAP
    assert fitted["seen"] is None and fitted["path"] is None
    assert len(nearest["differs"]) + nearest["differs_left_out"] == 1001
    # a long web path is cut as well
    web = engine.Call("http", "http:x.acme", "/p/" + "x" * 5000, method="GET")
    mocks = (Mock("http:x.acme", None, None, "GET", "/stock/1"),)
    q = Plan("run-1", "t", "row", mocks, (), (), (), 60.0)
    shown = state.miss_evidence(web, q, engine.missed(web, q, engine.Decision("real")))
    assert len(cast(str, shown["path"])) <= state.PATH_CUT


@pytest.mark.design("decide.miss_evidence")
def test_a_miss_line_is_kept_when_the_queue_is_full(lines: Lines) -> None:
    pinned(lines, {"order_id": "1234"})
    tool_gate.gate(lookup, "lookup")("5678")
    tool_gate.gate(lookup, "lookup")("1234")  # answered by the mock: nothing to keep
    assert lines.kept == [True, False]


@pytest.mark.design("decide.miss_evidence")
def test_a_refusal_holds_no_argument_value(lines: Lines) -> None:
    install(lines, [args_mock("tool:lookup", {"order_id": "1234"})])
    with pytest.raises(ScrimmageRefused) as caught:
        tool_gate.gate(lookup, "lookup")("order-5678")
    message = str(caught.value)
    assert "none matches this call's arguments" in message
    assert "order-5678" not in message and "1234" not in message
    assert 'add "tool:lookup" to real' in message
