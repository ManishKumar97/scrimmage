"""Tools that are methods on a class, listed as module:Class.method."""

import asyncio
import inspect
import json
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import config, labels, state, tool_gate
from scrimmage_core.config import Fault, Mock
from scrimmage_core.errors import ConfigError
from test_tool_gate import Lines, install

Fake = Callable[..., FakeOpenAI]

SOURCE = """
from dataclasses import dataclass

@dataclass
class Stock:
    sku: str
    count: int

class Base:
    def __init__(self, secret):
        self.secret = secret
        self.helper = lambda: "set in __init__"

    def get_order(self, order_id):
        return "real " + order_id

    async def get_order_async(self, order_id):
        return "real async " + order_id

    def get_stock(self, sku) -> Stock:
        return Stock(sku, 1)

    @staticmethod
    def check(sku):
        return "real static " + sku

    @staticmethod
    async def check_async(sku):
        return "real static async " + sku

    @classmethod
    def refund(cls, order_id):
        return "real class " + cls.__name__

    @classmethod
    async def refund_async(cls, order_id):
        return "real class async " + cls.__name__

    @property
    def status(self):
        return "a property"

    limit = 10

    class Inner:
        def go(self):
            return "inner"

class Sub(Base):
    pass

class Other(Base):
    pass

def plain(x):
    return x
"""


def toolkit() -> types.ModuleType:
    """Fresh classes for each test, as the gate changes the class it is set on."""
    module = types.ModuleType("shop_tools")
    exec(SOURCE, module.__dict__)
    return module


@pytest.fixture
def lines() -> Iterator[Lines]:
    yield Lines()
    state.install(None)


def secretless(lines: Lines) -> None:
    """No line holds self, cls or the object's state."""
    text = json.dumps(lines.lines)
    assert "s3cret" not in text and "object at 0x" not in text and "<class" not in text


@pytest.mark.design("method.every_instance")
def test_a_method_is_gated_on_instances_made_before_and_after(lines: Lines) -> None:
    module = toolkit()
    before = module.Base("s3cret")
    install(lines, [Mock("tool:get_order", "mocked", None)])
    tool_gate.swap(module, ["Base.get_order"])
    after = module.Base("s3cret")
    assert before.get_order("1") == "mocked"
    assert after.get_order(order_id="2") == "mocked"
    assert module.Base.get_order(after, "3") == "mocked"  # through the class, self passed in
    assert [line["call"] for line in lines.lines] == ["tool:get_order"] * 3
    previews = [str(line["args"]["preview"]) for line in lines.lines]  # type: ignore[index]
    assert previews == ['{"order_id": "1"}', '{"order_id": "2"}', '{"order_id": "3"}']
    secretless(lines)


@pytest.mark.design("method.kinds")
@pytest.mark.parametrize(
    ("name", "call", "real"),
    [
        ("check", lambda m: m.Base("s3cret").check("A"), "real static A"),
        ("check", lambda m: m.Base.check("A"), "real static A"),
        ("refund", lambda m: m.Base("s3cret").refund("1"), "real class Base"),
        ("refund", lambda m: m.Sub.refund("1"), "real class Sub"),
    ],
    ids=["static on instance", "static on class", "classmethod", "classmethod on subclass"],
)
def test_staticmethods_and_classmethods_keep_their_kind(
    lines: Lines, name: str, call: Callable[[Any], str], real: str
) -> None:
    module = toolkit()
    install(lines, real=["tool:*"])
    tool_gate.swap(module, [f"Base.{name}"])
    kind = staticmethod if name == "check" else classmethod
    assert isinstance(module.Base.__dict__[name], kind)
    assert call(module) == real
    assert lines.lines[0]["action"] == "real" and lines.lines[0]["call"] == f"tool:{name}"
    preview = str(lines.lines[0]["args"]["preview"])  # type: ignore[index]
    assert preview in ('{"sku": "A"}', '{"order_id": "1"}')
    secretless(lines)


@pytest.mark.design("method.async")
@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("get_order_async", lambda m: m.Base("s3cret").get_order_async("1")),
        ("check_async", lambda m: m.Base.check_async("1")),
        ("refund_async", lambda m: m.Sub("s3cret").refund_async("1")),
    ],
    ids=["method", "staticmethod", "classmethod"],
)
def test_async_methods_get_an_async_gate(
    lines: Lines, name: str, call: Callable[[Any], Any]
) -> None:
    module = toolkit()
    tool_gate.swap(module, [f"Base.{name}"])
    function = getattr(module.Base.__dict__[name], "__func__", module.Base.__dict__[name])
    assert inspect.iscoroutinefunction(function)
    install(lines, [Mock(f"tool:{name}", "mocked", None)])
    assert asyncio.run(call(module)) == "mocked"
    install(lines, faults=[Fault(f"tool:{name}", "timeout", "always")])
    with pytest.raises(TimeoutError, match=f"{name} timed out"):
        asyncio.run(call(module))
    install(lines, real=["tool:*"])
    assert asyncio.run(call(module)).startswith("real")
    secretless(lines)


@pytest.mark.design("method.faults_as_functions")
def test_faults_and_mocks_on_a_method_behave_as_on_a_function(lines: Lines) -> None:
    module = toolkit()
    tool_gate.swap(module, ["Base.get_order", "Base.get_stock"])
    shop = module.Base("s3cret")
    install(lines, faults=[Fault("tool:get_order", "error", "always")])
    with pytest.raises(RuntimeError, match="get_order failed"):
        shop.get_order("1")
    install(lines, faults=[Fault("tool:get_order", "error", "always", raises="ConnectionError")])
    with pytest.raises(ConnectionError):
        shop.get_order("1")
    install(lines, [Mock("tool:get_stock", {"sku": "SKU-42", "count": 12}, None)])
    stock = shop.get_stock("SKU-42")
    assert type(stock) is module.Stock and stock.count == 12  # from the return annotation
    install(lines)  # neither mocked nor real: refused, and the real method never runs
    with pytest.raises(Exception, match="neither mocked by this test nor listed"):
        shop.get_order("1")
    secretless(lines)


@pytest.mark.design("method.base_class")
def test_a_method_from_a_base_class_is_gated_on_the_named_subclass_only(lines: Lines) -> None:
    module = toolkit()
    install(lines, [Mock("tool:get_order", "mocked", None)])
    tool_gate.swap(module, ["Sub.get_order"])
    assert module.Sub("s3cret").get_order("1") == "mocked"
    assert type("Deeper", (module.Sub,), {})("s3cret").get_order("1") == "mocked"
    assert module.Base("s3cret").get_order("1") == "real 1"  # not touched, and not recorded
    assert module.Other("s3cret").get_order("1") == "real 1"
    assert "get_order" not in module.Base.__dict__ or not tool_gate.is_gate(
        module.Base.__dict__["get_order"]
    )
    assert len(lines.lines) == 2


@pytest.mark.design("method.two_classes")
def test_one_method_listed_on_a_base_and_a_subclass_keeps_each_name(lines: Lines) -> None:
    module = toolkit()
    install(lines, real=["tool:*"])
    tool_gate.swap(module, ["Base.get_order", "Sub.get_order"], {"shop_tools:Sub.get_order": "sub"})
    module.Base("s3cret").get_order("1")
    module.Sub("s3cret").get_order("1")
    assert [line["call"] for line in lines.lines] == ["tool:get_order", "tool:sub"]
    tool_gate.swap(module, ["Base.get_order", "Sub.get_order"])  # a second swap wraps nothing
    module.Sub("s3cret").get_order("1")
    assert len(lines.lines) == 3


def test_a_method_wrapped_by_the_teams_own_decorator_stays_wrapped(lines: Lines) -> None:
    import functools

    module = toolkit()
    calls: list[str] = []
    real = module.Base.get_order

    @functools.wraps(real)
    def logged(self: Any, order_id: str) -> str:
        calls.append(order_id)
        return real(self, order_id)

    module.Base.get_order = logged
    install(lines, real=["tool:*"])
    tool_gate.swap(module, ["Base.get_order", "Sub.get_order"], {"shop_tools:Sub.get_order": "s"})
    assert module.Sub("s3cret").get_order("1") == "real 1"
    assert calls == ["1"]  # the decorator ran, once
    assert [line["call"] for line in lines.lines] == ["tool:s"]


@pytest.mark.design("method.refused_at_load")
@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("Base.status", "it is a property, so it cannot be gated as a tool"),
        ("Base.limit", "it is not a method, so it cannot be gated as a tool"),
        ("Base.Inner", "it is a nested class, so it cannot be gated as a tool"),
        ("Base.helper", "Base has no method helper; an attribute set in __init__ cannot be"),
        ("Missing.get_order", "its module has no such class"),
        ("plain.get_order", "shop_tools:plain is not a class"),
    ],
    ids=["property", "plain attribute", "nested class", "set in init", "no class", "function"],
)
def test_a_name_that_cannot_be_gated_stops_the_run_at_the_import(
    lines: Lines, name: str, message: str
) -> None:
    module = toolkit()
    install(lines, real=["tool:*"])
    with pytest.raises(tool_gate.ListedToolMissing, match=message):
        tool_gate.swap(module, [name])
    assert lines.lines[0]["action"] == "refused" and message in str(lines.lines[0]["mistake"])


@pytest.mark.design("method.class_is_not_a_tool")
def test_a_class_listed_as_a_tool_is_refused_with_the_form_to_use(lines: Lines) -> None:
    module = toolkit()
    install(lines, real=["tool:*"])
    with pytest.raises(tool_gate.ListedToolMissing) as caught:
        tool_gate.swap(module, ["Base"])
    assert "it is a class" in str(caught.value)
    assert "shop_tools:Base.method" in str(caught.value)
    assert module.Base("s3cret").get_order("1") == "real 1"  # nothing was recorded as a tool
    assert lines.lines[0]["call"] == "tool:Base" and lines.lines[0]["action"] == "refused"


def test_a_missing_class_in_a_file_imported_under_another_name_is_left_alone() -> None:
    tool_gate.swap(types.ModuleType("tools"), ["Missing.get_order"], listed="shop.tools")


@pytest.mark.design("method.no_self_in_record")
def test_self_is_left_out_even_when_the_arguments_do_not_bind(lines: Lines) -> None:
    module = toolkit()
    install(lines, [Mock("tool:get_order", "m", None)])
    tool_gate.swap(module, ["Base.get_order"])
    assert module.Base("s3cret").get_order("1", "extra", more=2) == "m"
    preview = json.loads(str(lines.lines[0]["args"]["preview"]))  # type: ignore[index]
    assert preview == {"args": ["1", "extra"], "kwargs": {"more": 2}}
    secretless(lines)


@pytest.mark.design("method.config")
def test_a_method_path_is_read_and_the_model_sees_the_methods_name(tmp_path: Path) -> None:
    text = (Path(__file__).parent / "example" / "scrimmage.toml").read_text()
    text = text.replace('"acme.tools:get_order"', '"acme.toolkit:StoreTools.get_order"')
    text = text.replace(
        '"acme.tools:check_inventory"',
        '{path = "acme.toolkit:StoreTools.check_inventory", name = "inventory"}',
    ).replace('real     = ["tool:check_inventory"]', 'real     = ["tool:inventory"]')
    (tmp_path / "scrimmage.toml").write_text(text)
    agent = config.load(tmp_path / "scrimmage.toml").agent
    assert agent.tools[0] == "acme.toolkit:StoreTools.get_order"
    assert agent.tool_names()[:2] == ["get_order", "inventory"]
    assert labels.tool_name("a.b:C.d", {}) == "d"


@pytest.mark.design("method.nested_refused_in_file")
@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("acme.toolkit:Outer.Inner.go", "names a method on a nested class"),
        ("acme.toolkit:StoreTools.", "is not an import path"),
        ("acme.toolkit:.go", "is not an import path"),
        ("acme toolkit:StoreTools.go", "is not an import path"),
        ("StoreTools.go", "is not an import path"),
    ],
)
def test_a_bad_method_path_is_rejected_when_the_file_is_read(
    tmp_path: Path, path: str, message: str
) -> None:
    text = (Path(__file__).parent / "example" / "scrimmage.toml").read_text()
    (tmp_path / "scrimmage.toml").write_text(text.replace("acme.tools:refund_order", path))
    with pytest.raises(ConfigError, match=message):
        config.load(tmp_path / "scrimmage.toml")


def test_two_classes_with_one_method_name_need_a_name_each(tmp_path: Path) -> None:
    text = (Path(__file__).parent / "example" / "scrimmage.toml").read_text()
    text = text.replace("acme.tools:refund_order", "acme.toolkit:OrderTools.get_order")
    text = text.replace("acme.tools:get_order", "acme.toolkit:StoreTools.get_order")
    (tmp_path / "scrimmage.toml").write_text(text)
    with pytest.raises(ConfigError, match="two tools are named 'get_order'"):
        config.load(tmp_path / "scrimmage.toml")


TOOLKIT = (
    'tools    = ["acme.toolkit:StoreTools.get_order", "acme.toolkit:StoreTools.check_inventory", '
    '"acme.toolkit:StoreTools.refund_order", "acme.toolkit:StoreTools.get_stock"]'
)


@pytest.mark.design("method.end_to_end")
def test_a_toolkit_agent_runs_with_its_methods_mocked_faulted_and_kept_secret(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('function = "acme.agent:answer"', 'function = "acme.toolkit:answer"\nruns = 2')
    text = project.toml.read_text()
    old = next(line for line in text.splitlines() if line.startswith("tools"))
    project.edit(old, TOOLKIT)
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out
    runs = result.records()
    assert len(runs) == 4  # two baseline runs, two fault runs
    calls = [x for run in runs for x in run if x.get("kind") == "tool"]
    assert {x["call"] for x in calls} == {"tool:get_order", "tool:get_stock"}
    assert {x["action"] for x in calls} == {"mock", "fault"}
    faulted = [x for x in calls if x["action"] == "fault"]
    assert len(faulted) == 2 and all(
        x["error"] == "TimeoutError: get_order timed out" for x in faulted
    )
    for path in (result.root / "scrimmage-runs").glob("*/*.jsonl"):
        assert "s3cret" not in path.read_text()


# The review's probes, from test_probe_methods.py.
PROBES = """
import functools

class Base:
    def __init__(self):
        self.secret = "s3cret"

    def get(self, order_id):
        return "real " + order_id

    fetch = get

    def star(*args):
        return "star " + repr(args[1:])

    @functools.lru_cache
    def cached(self, x):
        return x

class Sub(Base):
    pass

class Meta(type):
    def __setattr__(cls, key, value):
        raise TypeError("frozen class")

class Frozen(metaclass=Meta):
    def get(self, x):
        return x
"""


def probes() -> types.ModuleType:
    module = types.ModuleType("probe_tools")
    exec(PROBES, module.__dict__)
    return module


@pytest.mark.design("method.alias_gated")
def test_an_alias_in_the_class_body_goes_through_the_gate_too(lines: Lines) -> None:
    module = probes()
    install(lines, [Mock("tool:get", "mocked", None)])
    tool_gate.swap(module, ["Base.get"])
    shop = module.Base()
    assert shop.get("1") == "mocked" and shop.fetch("1") == "mocked"
    assert [line["call"] for line in lines.lines] == ["tool:get", "tool:get"]


def test_an_alias_on_a_base_class_is_gated_through_the_named_subclass(lines: Lines) -> None:
    module = probes()
    install(lines, [Mock("tool:get", "mocked", None)])
    tool_gate.swap(module, ["Sub.get"])
    assert module.Sub().fetch("1") == "mocked"
    assert module.Base().fetch("1") == "real 1"  # the base class is left alone


@pytest.mark.design("method.star_args")
def test_a_method_on_star_args_drops_only_self(lines: Lines) -> None:
    module = probes()
    install(lines, real=["tool:star"])
    tool_gate.swap(module, ["Base.star"])
    assert module.Base().star("a", "b") == "star ('a', 'b')"
    preview = json.loads(str(lines.lines[0]["args"]["preview"]))  # type: ignore[index]
    assert preview == {"args": ["a", "b"]}
    secretless(lines)


@pytest.mark.design("method.frozen_class")
def test_a_class_that_refuses_the_gate_stops_the_run_with_the_reason(lines: Lines) -> None:
    module = probes()
    install(lines)
    with pytest.raises(tool_gate.ListedToolMissing, match="does not let Scrimmage set the gate"):
        tool_gate.swap(module, ["Frozen.get"])
    assert "frozen class" in str(lines.lines[0]["mistake"])


def test_a_cached_method_is_refused_as_not_a_method(lines: Lines) -> None:
    module = probes()
    install(lines)
    with pytest.raises(tool_gate.ListedToolMissing, match="it is not a method"):
        tool_gate.swap(module, ["Base.cached"])


def test_a_subclass_then_its_base_each_keep_their_gate(lines: Lines) -> None:
    module = probes()
    install(lines, real=["tool:get"])
    tool_gate.swap(module, ["Sub.get"])
    tool_gate.swap(module, ["Base.get"])
    module.Sub().get("1")
    module.Base().get("2")
    assert [line["call"] for line in lines.lines] == ["tool:get", "tool:get"]


def test_self_passed_by_name_or_with_extra_arguments_is_never_recorded(lines: Lines) -> None:
    module = probes()
    install(lines, real=["tool:get"])
    tool_gate.swap(module, ["Base.get"])
    shop = module.Base()
    module.Base.get(self=shop, order_id="1")
    with pytest.raises(TypeError):
        shop.get("1", "extra")
    secretless(lines)


@pytest.mark.design("method.alias_listed_by_name")
@pytest.mark.parametrize("order", [["Base.get", "Base.fetch"], ["Base.fetch", "Base.get"]])
def test_an_alias_listed_under_its_own_name_keeps_that_name(lines: Lines, order: list[str]) -> None:
    module = probes()
    install(lines, [Mock("tool:get", "from get", None), Mock("tool:fetch", "from fetch", None)])
    tool_gate.swap(module, order)
    shop = module.Base()
    assert shop.get("1") == "from get" and shop.fetch("1") == "from fetch"
    assert [line["call"] for line in lines.lines] == ["tool:get", "tool:fetch"]
