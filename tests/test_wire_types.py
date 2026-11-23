"""Fields and Reply hide a parsed request or reply from every module that is not a provider.

They are real classes, not a dict under another name, so the run-time ways around the type
checker fail too: isinstance, getattr, repr and a copy see nothing of the content."""

import ast
import copy
import pickle
from pathlib import Path

import pytest

import scrimmage
from scrimmage.providers.base import NO_FIELDS, Fields, Reply
from scrimmage.providers.impl.wire import fields_dict, reply_value, wrap_fields

SECRET = {"model": "m", "stream": True, "messages": [{"content": "sk-secret-text"}]}


def opaque(kind: type) -> object:
    return Fields(SECRET) if kind is Fields else Reply(SECRET)


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_an_opaque_wire_value_is_no_dict_and_has_no_dict_methods(kind: type) -> None:
    held = opaque(kind)
    assert not isinstance(held, dict)
    for name in ("get", "items", "keys", "values", "pop", "setdefault", "copy", "__getitem__"):
        with pytest.raises(AttributeError):
            getattr(held, name)
    for use in (lambda: len(held), lambda: list(held), lambda: held["model"], lambda: "m" in held):  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            use()


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_an_opaque_wire_value_shows_none_of_its_content(kind: type) -> None:
    held = opaque(kind)
    for shown in (repr(held), str(held), f"{held}", format(held)):
        assert "secret" not in shown
        assert "stream" not in shown
        assert "model" not in shown
    assert not hasattr(held, "__dict__"), "a slot class has nowhere else to keep the content"
    assert held != SECRET


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_an_opaque_wire_value_is_a_final_class_with_one_slot(kind: type) -> None:
    assert kind.__slots__ == ("_wire_value",)  # type: ignore[attr-defined]
    assert getattr(kind, "__final__", False), "typing.final marks it"


def test_a_provider_opens_what_it_wrapped_and_nothing_else_does() -> None:
    wrapped = wrap_fields(dict(SECRET))
    assert fields_dict(wrapped) == SECRET
    assert fields_dict(None) == {}
    assert fields_dict(NO_FIELDS) == {}
    assert reply_value(Reply([1, 2])) == [1, 2]
    assert reply_value(Reply(None)) is None


def test_the_helpers_that_open_the_wire_types_are_not_in_the_interface_module() -> None:
    from scrimmage.providers import base

    for name in ("fields_dict", "wrap_fields", "reply_value"):
        assert not hasattr(base, name), f"{name} would be one import away from the gate"


PRIVATE_USAGE_ON = (
    "http_gate.py",
    "asgi_gate.py",
    "mcp_gate.py",
    "langchain_gate.py",
    "threadctx.py",
)


@pytest.mark.parametrize("module", PRIVATE_USAGE_ON)
def test_a_module_checked_as_standard_has_private_usage_on_so_the_slot_is_a_type_error(
    module: str,
) -> None:
    """pyright's standard mode leaves private usage off. A header that turns it on keeps
    `fields._wire_value` an error in each module that is checked as standard."""
    header = (Path(scrimmage.__file__).parent / module).read_text().splitlines()[0]
    assert "reportPrivateUsage=true" in header, module


def test_every_module_that_is_checked_as_standard_turns_private_usage_on() -> None:
    """A new module with the standard header and no such line would open the slot."""
    src = Path(scrimmage.__file__).parent
    standard = {
        path.name for path in src.glob("*.py") if path.read_text().startswith("# pyright: standard")
    }
    off = {
        name
        for name in standard
        if "reportPrivateUsage=true" not in (src / name).read_text().splitlines()[0]
    }
    assert not off, f"{sorted(off)} are checked as standard with private usage off"


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_an_opaque_wire_value_cannot_be_pickled_copied_or_reduced(kind: type) -> None:
    held = opaque(kind)
    for use in (
        lambda: pickle.dumps(held),
        lambda: pickle.dumps(held, protocol=0),
        lambda: copy.copy(held),
        lambda: copy.deepcopy(held),
        lambda: held.__reduce__(),  # type: ignore[attr-defined]
        lambda: held.__reduce_ex__(2),  # type: ignore[attr-defined]
        lambda: held.__getstate__(),  # type: ignore[attr-defined]
    ):
        with pytest.raises(TypeError, match="opaque"):
            use()


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_reflection_by_the_slot_name_is_the_providers_door_and_the_guard_forbids_it_elsewhere(
    kind: type,
) -> None:
    """The slot is reachable by name at run time, which is why no product module may write it."""
    held = opaque(kind)
    assert object.__getattribute__(held, "_wire_value") == SECRET


def test_wire_defines_exactly_its_three_helpers() -> None:
    """wire.py turns private usage off for the whole file: it must stay the three helpers."""
    from scrimmage.providers.impl import wire

    path = Path(wire.__file__)
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            continue
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            raise AssertionError(f"wire.py holds a statement that is no helper: {ast.dump(node)}")
    assert names == {"fields_dict", "wrap_fields", "reply_value"}


@pytest.mark.parametrize("kind", [Fields, Reply])
def test_a_table_module_that_is_handed_an_opaque_value_for_a_dict_says_so(kind: type) -> None:
    """_obj(x) turns anything that is no dict into {}, so a Fields passed in place of its dict
    would read as an empty request. It fails loudly instead."""
    from scrimmage import provider_openai

    with pytest.raises(TypeError, match="opaque"):
        provider_openai._obj(opaque(kind))  # pyright: ignore[reportPrivateUsage]
    assert provider_openai._obj({"a": 1}) == {"a": 1}  # pyright: ignore[reportPrivateUsage]
    assert provider_openai._obj(None) == {}  # pyright: ignore[reportPrivateUsage]
