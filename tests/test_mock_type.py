"""The mock type check: a mock whose value is not what its tool is declared to return gets
one warning that names the fix, once per test and mock, and never changes the exit code."""

import functools
import json
import shutil
import tomllib
import types
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import OK, ROW_FAILED, Project, Result
from scrimmage import changes, mocks, runner
from scrimmage.cli import main
from scrimmage.outcomes import MISTAKE

# Every test here, or nearly, runs scrimmage test, which starts the agent's process.
pytestmark = pytest.mark.slow

SHAPES = '''
import dataclasses
import functools
import enum
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple, NewType, Optional
from collections.abc import Generator, Iterator
from typing import Protocol, TypedDict, TypeVar

import pydantic
import typing_extensions

if TYPE_CHECKING:
    from acme.elsewhere import Missing


@dataclasses.dataclass
class Stock:
    sku: str
    count: int


@dataclasses.dataclass
class StockV2(Stock):
    shelf: str = "A1"


@dataclasses.dataclass
class Error:
    code: str


@dataclasses.dataclass
class Order:
    id: int


@dataclasses.dataclass
class Invoice:
    number: str


class Model(pydantic.BaseModel):
    sku: str


class Pair(NamedTuple):
    sku: str
    count: int


class Status(enum.Enum):
    SHIPPED = "shipped"
    PENDING = "pending"


class Row(TypedDict):
    sku: str


class OldRow(typing_extensions.TypedDict):  # as pydantic asks for before Python 3.12
    sku: str


class Marker:
    """An annotation object whose text cannot be shown."""

    def __repr__(self) -> str:
        raise RuntimeError("no text")

    __str__ = __repr__


MARK = Marker()


class RaisingEq(type):
    def __eq__(cls, other):
        raise RuntimeError("no eq")

    __hash__ = type.__hash__


class Compared(metaclass=RaisingEq):
    pass


class RaisingAttr(type):
    def __getattr__(cls, name):
        raise RuntimeError("no attr " + name)


class Odd(metaclass=RaisingAttr):
    pass


class RaisingCheck(type):
    def __instancecheck__(cls, obj):
        raise RuntimeError("no instance check")


class Checked(metaclass=RaisingCheck):
    pass


class BadSkuError(Exception):
    pass


@dataclasses.dataclass
class BadSku:
    sku: str

    def __post_init__(self):
        raise BadSkuError("rejected sku-secret-value")


class Keyed(pydantic.BaseModel):
    api_key: str = pydantic.Field(pattern="^pk-")


@dataclasses.dataclass
class PlainKey:
    api_key: str

    def __post_init__(self):
        raise ValueError("bad key " + self.api_key)


@dataclasses.dataclass
class Looked:
    sku: str

    def __post_init__(self):
        self.name = lookup(self.sku)  # a listed tool, called while the mock is built


def accepts_aliases(init):
    """A decorator that takes an old keyword name: the signature shows the inner one only."""

    @functools.wraps(init)
    def wrapper(self, **kwargs):
        if "sku_id" in kwargs:
            kwargs["sku"] = kwargs.pop("sku_id")
        return init(self, **kwargs)

    return wrapper


class Aliased:
    @accepts_aliases
    def __init__(self, sku: str) -> None:
        self.sku = sku


class Sized(Protocol):
    def size(self) -> int: ...


UserId = NewType("UserId", int)
T = TypeVar("T")


def text(key: str) -> str: ...
def integer(key: str) -> int: ...
def real(key: str) -> float: ...
def names(key: str) -> list[str]: ...
def table(key: str) -> dict: ...
def stock(key: str) -> Stock: ...
def model(key: str) -> Model: ...
def pair(key: str) -> Pair: ...
def maybe(key: str) -> Stock | None: ...
def optional(key: str) -> Optional[Stock]: ...
def status(key: str) -> Status: ...
def row(key: str) -> Row: ...
def couple(key: str) -> tuple[str, dict]: ...
def anything(key: str) -> Any: ...
def bare(key): ...
def literal(key: str) -> Literal["a", "b"]: ...
def thing(key: str) -> object: ...
def same(key: T) -> T: ...
def user(key: str) -> UserId: ...
def sized(key: str) -> Sized: ...
def counted(key: str) -> Annotated[int, "a count"]: ...
def invoice(key: str) -> bytes: ...
def orders(key: str) -> list[Order]: ...
def either(key: str) -> Stock | Error: ...
def hidden(key: str) -> "Missing": ...
def nothing(key: str) -> None: ...
def lines(key: str) -> Iterator[str]: ...
def made(key: str) -> Generator[str, None, None]: ...
def old_row(key: str) -> OldRow: ...
def marked(key: str) -> MARK: ...
def compared(key: str) -> Compared: ...
def odd(key: str) -> Odd: ...
def checked(key: str) -> Checked: ...
def bad_sku(key: str) -> BadSku: ...
def keyed(key: str) -> Keyed: ...
def plain_key(key: str) -> PlainKey: ...
def looked(key: str) -> Looked: ...
def lookup(key: str) -> str: ...
def aliased(key: str) -> Aliased: ...


def a_table() -> dict:
    return {"sku": "a"}


def a_stock() -> Stock:
    return Stock("SKU-1", 1)


def some_text() -> str:
    return "%PDF"


def answer(question: str) -> str:
    """The question names the tool to call, and how many times."""
    name, _, times = question.partition(" ")
    values = [globals()[name]("1") for _ in range(int(times or 1))]
    return f"ok {values[0]!r}"
'''

# Each tool's real body would raise: under a mock it never runs.
REAL_BODY = "raise AssertionError('the real tool ran')"


def write(project: Project, tool: str, mock: str, *, runs: int = 1, times: int = 1) -> None:
    (project.root / "acme" / "shapes.py").write_text(
        SHAPES.replace(": ...\n", f": {REAL_BODY}\n").replace(
            "def size(self) -> int: " + REAL_BODY, "def size(self) -> int: ..."
        )
    )
    question = tool if times == 1 else f"{tool} {times}"
    project.toml.write_text(
        f'[agent]\nfunction     = "acme.shapes:answer"\ntools        = ["acme.shapes:{tool}"]\n'
        f'runs         = {runs}\nmax_cost_usd = "none"\n\n'
        f'[[test]]\nname     = "shape"\nquestion = "{question}"\nexpect   = ["ok"]\n\n'
        f'[[test.mock]]\ncall    = "tool:{tool}"\n{mock}\n'
    )


def said(result: Result) -> list[str]:
    """The report's warnings, notes and mistakes, but the note every model-free run gets."""
    return [
        line
        for line in (result.out + result.err).splitlines()
        if line.startswith(("warning:", "note:", "mistake", "error"))
        and "answered with no model call" not in line
    ]


def notices(result: Result) -> list[dict[str, object]]:
    return [line for run in result.records() for line in run if line.get("notice") == "mock_type"]


VALID = {
    "str with text": ("text", 'returns = "shipped"'),
    "int with an integer": ("integer", "returns = 3"),
    "float with an integer": ("real", "returns = 3"),
    "list[str] with a list": ("names", 'returns = ["a", "b"]'),
    "dict with a table": ("table", "returns = {a = 1}"),
    "a dataclass with a table": ("stock", 'returns = {sku = "a", count = 1}'),
    "a pydantic model with a table": ("model", 'returns = {sku = "a"}'),
    "a NamedTuple with a table": ("pair", 'returns = {sku = "a", count = 1}'),
    "Stock | None with a table": ("maybe", 'returns = {sku = "a", count = 1}'),
    "Optional[Stock] with a table": ("optional", 'returns = {sku = "a", count = 1}'),
    "an Enum with a member's value": ("status", 'returns = "shipped"'),
    "a TypedDict with a table": ("row", 'returns = {sku = "a"}'),
    "tuple[str, dict] with a list": ("couple", 'returns = ["a", {b = 1}]'),
    "Any": ("anything", "returns = {a = 1}"),
    "no annotation": ("bare", "returns = {a = 1}"),
    "a Literal": ("literal", "returns = {a = 1}"),
    "object": ("thing", "returns = {a = 1}"),
    "a type variable": ("same", "returns = {a = 1}"),
    "a NewType": ("user", 'returns = "a"'),
    "a Protocol": ("sized", 'returns = {sku = "a", count = 1}\ntype = "acme.shapes:Stock"'),
    "Annotated[int] with an integer": ("counted", "returns = 3"),
    "a bool for int": ("integer", "returns = true"),
    "type naming a subclass": (
        "stock",
        'returns = {sku = "a", count = 1}\ntype = "acme.shapes:StockV2"',
    ),
    "a recorded mock": ("stock", 'returns = {sku = "a", count = 1}\nrecorded = true'),
    "returns_from giving the declared class": ("stock", 'returns_from = "acme.shapes:a_stock"'),
    "type on an unresolved return": (
        "hidden",
        'returns = {sku = "a", count = 1}\ntype = "acme.shapes:Stock"',
    ),
    # A table from returns_from on an unresolved return: type is refused beside it.
    "returns_from on an unresolved return": ("hidden", 'returns_from = "acme.shapes:a_table"'),
    # TOML cannot write None, so a write tool's mock says something else.
    "None with text": ("nothing", 'returns = "sent"'),
    "None with a table": ("nothing", "returns = {}"),
    # An agent loops over a list as over the real iterator.
    "an Iterator with a list": ("lines", 'returns = ["a", "b"]'),
    "a Generator with a list": ("made", 'returns = ["a", "b"]'),
    "a typing_extensions TypedDict with a table": ("old_row", 'returns = {sku = "a"}'),
    # The class is called before its signature is read: a decorator can take more keys.
    "a decorated __init__ taking an old key name": ("aliased", 'returns = {sku_id = "a"}'),
}


@pytest.mark.design("mock.type_check_valid")
@pytest.mark.parametrize(("tool", "mock"), list(VALID.values()), ids=list(VALID))
def test_a_valid_mock_gets_no_line(project: Project, tool: str, mock: str) -> None:
    write(project, tool, mock)
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert said(result) == []
    assert notices(result) == []


WRONG = {
    "a table for str": (
        "text",
        "returns = {a = 1}",
        "returns a table, but text is declared to return str; write returns as text, or fix "
        "the annotation",
    ),
    "text for int": (
        "integer",
        'returns = "3"',
        "returns text, but integer is declared to return int; write returns as an integer, or "
        "fix the annotation",
    ),
    "a list for dict": (
        "table",
        "returns = [1, 2]",
        "returns a list, but table is declared to return dict; write returns as a table, or fix "
        "the annotation",
    ),
    # Text for a TypedDict is refused as it is built, so a type is the way to a wrong one.
    "type naming a class for a TypedDict": (
        "row",
        'returns = {sku = "a", count = 1}\ntype = "acme.shapes:Stock"',
        "builds acme.shapes:Stock, but row is declared to return Row; fix the mock's type, or "
        "the annotation",
    ),
    "type naming a class for a typing_extensions TypedDict": (
        "old_row",
        'returns = {sku = "a", count = 1}\ntype = "acme.shapes:Stock"',
        "builds acme.shapes:Stock, but old_row is declared to return OldRow; fix the mock's "
        "type, or the annotation",
    ),
    "table items for list[Order]": (
        "orders",
        "returns = [{id = 1}, {id = 2}]",
        "keeps its items as tables, but orders is declared to return list[Order]; use "
        "returns_from to build them, or fix the annotation",
    ),
    "a table for Stock | Error": (
        "either",
        'returns = {sku = "a", count = 1}',
        "returns a table, but either is declared to return Stock | Error; name the class to "
        "build with type, or fix the annotation",
    ),
    "type naming an unrelated class": (
        "stock",
        'returns = {number = "INV-7"}\ntype = "acme.shapes:Invoice"',
        "builds acme.shapes:Invoice, but stock is declared to return Stock; fix the mock's "
        "type, or the annotation",
    ),
    "returns_from giving str for bytes": (
        "invoice",
        'returns_from = "acme.shapes:some_text"',
        "returns_from acme.shapes:some_text gives a str, but invoice is declared to return "
        "bytes; fix the function, or the annotation",
    ),
}


@pytest.mark.design("mock.type_check_wrong")
@pytest.mark.parametrize(("tool", "mock", "fix"), list(WRONG.values()), ids=list(WRONG))
def test_a_wrong_mock_gets_one_warning_naming_the_fix(
    project: Project, tool: str, mock: str, fix: str
) -> None:
    write(project, tool, mock)
    result = project.test()
    assert said(result) == [f"warning: in test 'shape' the mock on tool:{tool} {fix} (1 of 1 runs)"]
    assert result.code == OK, result.out + result.err


@pytest.mark.design("mock.type_check_warns_only")
def test_a_wrong_mock_leaves_the_exit_code_alone(project: Project) -> None:
    write(project, "text", "returns = {a = 1}")
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert [line[:40] for line in said(result)] == ["warning: in test 'shape' the mock on too"]
    # A row that fails still fails with its own code, not a mistake's.
    project.edit('expect   = ["ok"]', 'expect   = ["never said"]')
    result = project.test()
    assert result.code == ROW_FAILED, result.out + result.err
    assert any(line.startswith("warning: in test 'shape'") for line in said(result))


@pytest.mark.design("mock.type_check_type")
def test_a_type_that_is_not_the_annotation_is_warned(project: Project) -> None:
    write(project, "maybe", 'returns = {number = "INV-7"}\ntype = "acme.shapes:Invoice"')
    result = project.test()
    assert said(result) == [
        "warning: in test 'shape' the mock on tool:maybe builds acme.shapes:Invoice, but maybe "
        "is declared to return Stock | None; fix the mock's type, or the annotation "
        "(1 of 1 runs)"
    ]
    # A subclass of one of the members fits it.
    project.edit('"acme.shapes:Invoice"', '"acme.shapes:StockV2"')
    project.edit('returns = {number = "INV-7"}', 'returns = {sku = "a", count = 1}')
    result = project.test()
    assert result.code == OK and said(result) == [], result.out + result.err


@pytest.mark.design("mock.type_check_unresolved")
def test_an_untyped_table_on_an_unresolved_return_gets_the_note_naming_type(
    project: Project,
) -> None:
    write(project, "hidden", 'returns = {sku = "a", count = 1}')
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert said(result) == [
        "note: in test 'shape' hidden declares a return type Scrimmage cannot resolve "
        "('Missing'), so the mock is kept as a table; name the class with "
        'type = "module:Missing"'
    ]
    assert [line.get("note") for line in notices(result)] == [True]
    # Text is not kept as a table, so there is nothing to say.
    project.edit('returns = {sku = "a", count = 1}', 'returns = "a"')
    assert said(project.test()) == []


@pytest.mark.design("mock.type_check_once")
def test_the_warning_is_said_once_per_mock_with_its_run_count(project: Project) -> None:
    write(project, "text", "returns = {a = 1}", runs=3, times=5)
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert said(result) == [
        "warning: in test 'shape' the mock on tool:text returns a table, but text is declared "
        "to return str; write returns as text, or fix the annotation (3 of 3 runs)"
    ]
    calls = [line for run in result.records() for line in run if line.get("kind") == "tool"]
    assert len(calls) == 15  # five calls in each of three runs
    # One notice per run, not one per call.
    per_run = [sum(line.get("notice") == "mock_type" for line in run) for run in result.records()]
    assert per_run == [1, 1, 1]
    assert {line["mock"] for line in notices(result)} == {0}


RECORDED_TOOL = """
import dataclasses


@dataclasses.dataclass
class Stock:
    sku: str
    count: int


def get_stock(sku: str) -> Stock:
    return Stock(sku, 12)


def answer(question: str) -> str:
    return f"ok {get_stock('SKU-42').count}"
"""


@pytest.mark.design("mock.type_check_recorded")
def test_a_recorded_mock_gets_no_warning(project: Project) -> None:
    (project.root / "acme" / "stocked.py").write_text(RECORDED_TOOL)
    project.toml.write_text(
        '[agent]\nfunction     = "acme.stocked:answer"\ntools        = '
        '["acme.stocked:get_stock"]\nreal         = ["tool:get_stock"]\nmax_cost_usd = "none"\n\n'
        '[[test]]\nname     = "stock"\nquestion = "How many SKU-42?"\nexpect   = ["ok 12"]\n'
    )
    project.capsys.readouterr()
    assert main(["record", str(project.toml), "--json"]) == OK
    project.capsys.readouterr()
    recorded = project.root / "scrimmage.recorded.toml"
    (mock,) = tomllib.loads(recorded.read_text())["test"][0]["mock"]
    # A table built from the real Stock, which its annotation builds back.
    assert (mock["recorded"], mock["returns"]) == (True, {"sku": "SKU-42", "count": 12})
    assert "type" not in mock
    shutil.rmtree(project.root / "scrimmage-runs")  # so the records read are the test's own
    # The recorded file still lists the tool under real; the mock answers before it.
    code = main(["test", str(recorded)])
    captured = project.capsys.readouterr()
    result = Result(code, captured.out, captured.err, project.root)
    assert result.code == OK, result.out + result.err
    calls = [line for run in result.records() for line in run if line.get("kind") == "tool"]
    assert calls and {line["action"] for line in calls} == {"mock"}
    assert said(result) == [] and notices(result) == []


def report_tool(project: Project, annotation: str) -> None:
    """A LangChain tool with an artifact, whose mock TOML writes as a list of two."""
    (project.root / "acme" / "paired.py").write_text(
        "from langchain_core.tools import tool\n\n\n"
        '@tool(response_format="content_and_artifact")\n'
        f"def get_report(order_id: str) -> {annotation}:\n"
        '    """Get an order\'s report and its raw data."""\n'
        "    raise RuntimeError('the real reports system is not reachable from tests')\n\n\n"
        "def answer(question: str) -> str:\n"
        '    return "ok " + str(get_report.invoke({"order_id": "1"}))\n'
    )
    project.toml.write_text(
        '[agent]\nfunction     = "acme.paired:answer"\nruns         = 1\n'
        'max_cost_usd = "none"\n\n'
        '[[test]]\nname     = "report"\nquestion = "The report?"\nexpect   = ["ok the report"]\n\n'
        '[[test.mock]]\ncall    = "tool:get_report"\nreturns = ["the report", {a = "b"}]\n'
    )


@pytest.mark.design("mock.type_check_before_shape")
@pytest.mark.parametrize(
    "annotation",
    [
        "tuple[str, dict[str, str]]",
        # As TOML writes the pair: the list the team wrote fits it, the tuple LangChain gets
        # does not, so this one shows the check reads the value before the framework's shape.
        "list[str | dict[str, str]]",
    ],
)
def test_a_content_and_artifact_pair_gets_no_warning(project: Project, annotation: str) -> None:
    report_tool(project, annotation)
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert said(result) == [] and notices(result) == []
    calls = [line for run in result.records() for line in run if line.get("kind") == "tool"]
    assert [line["action"] for line in calls] == ["mock"]


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_an_error_in_the_check_never_breaks_the_call(project: Project) -> None:
    write(project, "marked", 'returns = {sku = "a"}')
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert said(result) == [] and notices(result) == []
    calls = [line for run in result.records() for line in run if line.get("kind") == "tool"]
    assert [(line["action"], line["error"]) for line in calls] == [("mock", None)]


# The rules the report lines rest on, on the function itself.


@pytest.mark.design("mock.type_check_rules")
def test_the_check_reads_annotated_typed_dicts_and_containers_as_the_contract_says() -> None:
    from typing import Annotated, TypedDict

    class Row(TypedDict):
        sku: str

    check = functools.partial(mocks.mismatch, kind=None, name="t")
    assert check({"sku": "a"}, declared=Annotated[Row, "x"]) is None
    assert check([1], declared=Annotated[dict[str, int], "x"]) is not None
    assert check([{"a": 1}], declared=list[str]) is None  # a plain item type is not read
    assert check([], declared=list[Path]) is None  # no first item
    assert check({"a": {"x": 1}}, declared=dict[str, Path]) is not None
    assert check(1, declared=float) is None and check(True, declared=int) is None
    assert check([1], declared=tuple[int, ...]) is None  # TOML has no tuple
    assert check(bytearray(b"x"), declared=bytes) is None and check("x", declared=bytes)
    assert mocks.fits({"a": 1}, changes.Unresolved("Missing")) is None
    assert check("x", declared=type(None)) is None and check("x", declared=str | None) is None
    assert check({"a": 1}, declared=str | None) is not None  # None in a union still reads X


@pytest.mark.design("mock.type_check_unresolved")
def test_annotations_that_raise_are_not_quoted_as_the_annotation() -> None:
    raising = changes.Unresolved("annotations that raise ZeroDivisionError")
    assert mocks.unresolved({"a": 1}, None, raising, "t") == (
        "t declares a return annotation that raises ZeroDivisionError, so the mock is kept as "
        'a table; name the class with type = "module:Class"'
    )
    assert mocks.unresolved({"a": 1}, None, raising, "t", "acme.m:f") is None


@pytest.mark.design("mock.type_check_once")
def test_a_mock_type_notice_without_its_mock_is_left_out_of_the_report() -> None:
    test = types.SimpleNamespace(name="t")
    line = {"notice": "mock_type", "message": "the mock on tool:x returns a table"}
    row = types.SimpleNamespace(test=test)
    result = types.SimpleNamespace(
        row=row, outcome="answered", run="r", notices=[line, {**line, "mock": 0}]
    )
    config = cast(Any, types.SimpleNamespace(tests=[test]))
    found = runner.mock_type_lines(config, cast(Any, [result]))
    assert found == (["in test 't' the mock on tool:x returns a table (1 of 1 runs)"], [])
    assert json.dumps(mocks.type_text(dict[str, list[Path]])) == '"dict[str, list[Path]]"'


# A class whose own hooks raise is the team's mistake: the run says so, and does not crash.


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_return_class_whose_eq_raises_still_builds(project: Project) -> None:
    write(project, "compared", "returns = {}")
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert "crashed" not in result.out + result.err


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_return_class_whose_getattr_raises_does_not_crash_the_run(project: Project) -> None:
    write(project, "odd", "returns = {a = 1}")
    result = project.test()
    assert result.code == OK, result.out + result.err
    assert "crashed" not in result.out + result.err


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_return_class_whose_instancecheck_raises_is_a_mistake_naming_the_error_class(
    project: Project,
) -> None:
    write(project, "checked", 'returns = "text"')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    assert (
        "the mock does not fit acme.shapes:Checked: the class's isinstance check raised "
        "RuntimeError"
    ) in shown
    leaked = "no instance check" in shown
    assert not leaked, "the error's message was quoted"


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_return_class_whose_post_init_raises_is_a_mistake_naming_the_error_class(
    project: Project,
) -> None:
    write(project, "bad_sku", 'returns = {sku = "a"}')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    assert "the mock does not fit acme.shapes:BadSku: building it raised BadSkuError" in shown
    leaked = "sku-secret-value" in shown
    assert not leaked, "the error's message was quoted"


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_an_explicit_type_whose_getattr_raises_is_a_mistake_naming_the_error_class(
    project: Project,
) -> None:
    write(project, "odd", 'returns = {a = 1}\ntype    = "acme.shapes:Odd"')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    assert "the mock does not fit acme.shapes:Odd: building it raised RuntimeError" in shown
    leaked = "no attr" in shown
    assert not leaked, "the error's message was quoted"


# A value the team's class rejects is never copied out of the mock: a known secret shape,
# which records redact, and one of no known shape, which nothing would redact.
REJECTED = ["sk-live-PROBESECRET-7f3a9c", "customer-ssn-123-45-6789"]


@pytest.mark.design("mock.type_check_never_breaks_a_call")
@pytest.mark.parametrize("value", REJECTED, ids=["secret shape", "unknown shape"])
@pytest.mark.parametrize(
    ("tool", "why"),
    [
        ("keyed", "acme.shapes:Keyed: ValidationError at api_key (string_pattern_mismatch)"),
        ("plain_key", "acme.shapes:PlainKey: building it raised ValueError"),
    ],
    ids=["pydantic", "dataclass"],
)
def test_a_value_the_class_rejects_is_in_no_report_record_or_log(
    project: Project, tool: str, why: str, value: str
) -> None:
    write(project, tool, f'returns = {{api_key = "{value}"}}')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    files = [f for f in (project.root / "scrimmage-runs").rglob("*") if f.is_file()]
    assert {f.suffix for f in files} >= {".json", ".jsonl", ".log"}, files
    found = [f.suffix for f in files if value in f.read_text(errors="replace")]
    leaked = value in shown or bool(found)
    assert not leaked, f"the mock's value was copied out, into {found or 'the report'}"
    assert f"the mock does not fit {why}" in shown


@pytest.mark.design("mock.type_check_never_breaks_a_call")
@pytest.mark.parametrize("value", REJECTED, ids=["secret shape", "unknown shape"])
def test_an_enum_value_that_is_no_member_is_in_no_report_record_or_log(
    project: Project, value: str
) -> None:
    write(project, "status", f'returns = "{value}"')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    root = project.root / "scrimmage-runs"
    files = [f for f in root.rglob("*") if f.is_file()]
    assert {f.suffix for f in files} >= {".json", ".jsonl", ".log"}, files
    found = [f.name for f in files if value in f.read_text(errors="replace")]
    leaked = value in shown or bool(found)
    assert not leaked, f"the mock's value was copied out, into {found or 'the report'}"
    assert (
        "the mock does not fit acme.shapes:Status: the value is not one of shipped, pending"
        in shown
    )


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_wrong_key_names_the_key_and_no_value(project: Project) -> None:
    write(project, "stock", 'returns = {sku = "a-hidden-sku", cnt = 1}')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    assert "the mock does not fit acme.shapes:Stock: missing a required argument: 'count'" in shown
    leaked = "a-hidden-sku" in shown
    assert not leaked, "the mock's value was quoted"


@pytest.mark.design("mock.type_check_never_breaks_a_call")
def test_a_refused_call_inside_the_class_is_not_blamed_on_the_class(project: Project) -> None:
    write(project, "looked", 'returns = {sku = "a"}')
    project.edit('["acme.shapes:looked"]', '["acme.shapes:looked", "acme.shapes:lookup"]')
    result = project.test()
    shown = result.out + result.err
    assert result.code == MISTAKE, shown
    assert "tool:lookup" in shown, shown
    assert "does not fit" not in shown, shown
