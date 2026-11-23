"""Fields and Reply hide a parsed request or reply from every module that is not a provider.

They are real classes, not a dict under another name, so the run-time ways around the type
checker fail too: isinstance, getattr, repr and a copy see nothing of the content."""

import copy
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
    assert kind.__slots__ == ("_value",)  # type: ignore[attr-defined]
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


def test_the_gate_is_checked_for_private_usage_so_x_dot_value_is_a_type_error_there() -> None:
    """The gate is checked as standard, which leaves private usage off. A line that turns it
    on keeps `fields._value` an error in the one module that holds the parsed request."""
    gate = Path(scrimmage.__file__).parent / "http_gate.py"
    assert "reportPrivateUsage=true" in gate.read_text().splitlines()[0]


def test_a_copy_of_an_opaque_value_is_still_opaque() -> None:
    held = Fields(SECRET)
    for again in (copy.copy(held), copy.deepcopy(held)):
        assert not isinstance(again, dict)
        assert "secret" not in repr(again)
