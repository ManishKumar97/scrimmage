"""Turn a mock's TOML value into what the tool really returns.

A value is turned into the tool's own type when it has one: the mock's type, or the
tool's return annotation when type is left out. A pydantic model is built with
model_validate, an Enum from its value, any other class with Type(**returns). A union of
one class and None builds that class from a table, as TOML cannot write None, and keeps any
other value as written. A value that does not fit is a mistake in the test. Each call gets
its own copy, so a change the agent makes to the value is not seen by the next call.
A value TOML cannot hold, such as bytes, is built by the team's own function: returns_from.
"""

import enum
import importlib
import inspect
import types
import typing
from collections.abc import Callable
from typing import cast

from scrimmage_core.errors import ScrimmageMockError, ScrimmageRefused

# Any is a class on 3.11 and later, and neither it nor object names a shape to build.
PLAIN = (str, int, float, bool, list, dict, tuple, type(None), object, typing.Any)
# The members a mistake names, so a large Enum gives a short line.
SHOWN_MEMBERS = 10


def allowed(path: str, prefixes: list[str]) -> bool:
    module = path.partition(":")[0]
    return any((module + ".").startswith(prefix) for prefix in prefixes)


def import_object(path: str, prefixes: list[str]) -> object:
    """Import module:attr, only under an allowed prefix, checked before importing."""
    if not allowed(path, prefixes):
        raise ScrimmageRefused(
            f"{path} is outside SCRIMMAGE_ALLOW_IMPORTS ({', '.join(prefixes) or 'empty'}), "
            "so it is not imported"
        )
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def return_type(tool: Callable[..., object]) -> object:
    """The tool's return annotation, resolved alone, so a parameter type that cannot be
    resolved is no matter. None when there is none or it cannot be resolved: as on Any, the
    mock is kept as written, and a mock's type names the class to build."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    declared = changes.declared_return(tool)
    return None if isinstance(declared, changes.Unresolved) else declared


def fresh(value: object) -> object:
    """A copy of a TOML value: only its lists and tables can change, so only they are copied.
    It is about a third of the cost of copy.deepcopy."""
    # This relies on the value holding only TOML and JSON types: dicts, lists and scalars.
    # Any other mutable object in it would be shared between calls.
    if type(value) is dict:
        return {key: fresh(item) for key, item in cast(dict[str, object], value).items()}
    if type(value) is list:
        return [fresh(item) for item in cast(list[object], value)]
    return value


def table(value: object) -> bool:
    """Whether the value is a TOML table. A plain bool, so the value's type is not narrowed."""
    return isinstance(value, dict)


def target_of(declared: object) -> object:
    """The class to build for a declared type: a union of one class and None is that class.
    Any other union keeps the value as written."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    if typing.get_origin(declared) not in (typing.Union, types.UnionType):
        return declared
    kinds = [arg for arg in changes.members(declared) if arg is not types.NoneType]
    return kinds[0] if len(kinds) == 1 else None


def build(
    returns: object, kind: str | None, tool: Callable[..., object], prefixes: list[str]
) -> object:
    declared = import_object(kind, prefixes) if kind else return_type(tool)
    target = target_of(declared)
    returns = fresh(returns)
    if target is not declared and not table(returns):
        # X | None builds X from a table only, so a Path's text stays text; an Enum's
        # member value builds the member, and any other value stays as written.
        if isinstance(target, enum.EnumMeta):
            return member_or_value(cast("type[enum.Enum]", target), returns)
        return returns
    if not isinstance(target, type) or target in PLAIN:
        return returns
    name = kind or f"{target.__module__}:{target.__qualname__}"
    if isinstance(target, enum.EnumMeta):  # an Enum class
        return member(cast("type[enum.Enum]", target), returns, name)
    try:
        validate = getattr(target, "model_validate", None)
        if callable(validate):
            return validate(returns)
        if isinstance(returns, dict):
            return target(**returns)
        if isinstance(returns, target):
            return returns
    except (TypeError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise ScrimmageMockError(f"the mock does not fit {name}: {exc}") from exc
    raise ScrimmageMockError(
        f"the mock does not fit {name}: expected a table of its fields; a value no table "
        "can build is built by a function named with returns_from"
    )


def produced(path: str, prefixes: list[str]) -> object:
    """A returns_from mock's value: the team's function, called with no arguments on every
    call, so a change the agent makes to one value is not seen by the next call. A function
    that cannot be imported, is async, or raises is a mistake. The mistake names the error's
    class only, and is raised outside the except block, so it holds no reference to the
    error: its message can quote a value. SystemExit is the team's error too; a
    KeyboardInterrupt still stops the run."""
    failed: str | None = None
    factory: object = None
    try:
        factory = import_object(path, prefixes)  # the prefix is checked before the import
    except ScrimmageRefused as exc:
        failed = f"returns_from {exc}"
    except (Exception, SystemExit) as exc:  # the module's own code, or a missing name
        failed = f"returns_from {path} cannot be imported: it raised {type(exc).__name__}"
    if failed is not None:
        raise ScrimmageMockError(failed)
    if inspect.iscoroutinefunction(factory) or inspect.isasyncgenfunction(factory):
        raise ScrimmageMockError(f"returns_from {path} must be a plain function, not async")
    if not callable(factory):
        raise ScrimmageMockError(f"returns_from {path} is not a function")
    value: object = None
    try:
        value = factory()
    except (Exception, SystemExit) as exc:  # the team's code: a broken test, not a broken agent
        failed = f"returns_from {path} raised {type(exc).__name__}"
    if failed is not None:
        raise ScrimmageMockError(failed)
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            close()  # a coroutine never awaited warns when it is collected
        raise ScrimmageMockError(
            f"returns_from {path} must be a plain function, not async: it returned an awaitable"
        )
    return value


def member_or_value(kind: type[enum.Enum], value: object) -> object:
    """The member with this value, or the value as written: under E | None, a value that is
    no member passed through before Enums were built, so it is never a mistake."""
    try:
        return kind(value)
    except (TypeError, ValueError):
        return value


def member(kind: type[enum.Enum], value: object, name: str) -> enum.Enum:
    """The member with this value, as the real tool makes it: Status("shipped")."""
    try:
        return kind(value)
    except (TypeError, ValueError):
        values = [str(m.value) for m in kind]
        shown = ", ".join(values[:SHOWN_MEMBERS])
        if len(values) > SHOWN_MEMBERS:
            shown += f", and {len(values) - SHOWN_MEMBERS} more"
        raise ScrimmageMockError(
            f"the mock does not fit {name}: {value!r} is not one of {shown}"
        ) from None
