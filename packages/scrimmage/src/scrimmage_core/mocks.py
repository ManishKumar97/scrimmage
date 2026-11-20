"""Turn a mock's TOML value into what the tool really returns.

A value is turned into the tool's own type when it has one: the mock's type, or the
tool's return annotation when type is left out. A pydantic model is built with
model_validate, an Enum from its value, any other class with Type(**returns). A union of
one class and None builds that class from a table, as TOML cannot write None, and keeps any
other value as written. A value that does not fit is a mistake in the test. Each call gets
its own copy, so a change the agent makes to the value is not seen by the next call.
A value TOML cannot hold, such as bytes, is built by the team's own function: returns_from.
A built value that does not fit the tool's return annotation gets a warning, never a mistake.
"""

import collections.abc
import enum
import importlib
import inspect
import sys
import types
import typing
from collections.abc import Callable
from typing import cast

from scrimmage_core.errors import ScrimmageError, ScrimmageMockError, ScrimmageRefused

# Any is a class on 3.11 and later, and neither it nor object names a shape to build.
PLAIN = (str, int, float, bool, list, dict, tuple, type(None), object, typing.Any)
# Results a list fits, as an agent loops over it: the check says nothing for them.
STREAMS = (
    collections.abc.Iterable,
    collections.abc.Iterator,
    collections.abc.Generator,
    collections.abc.AsyncIterable,
    collections.abc.AsyncIterator,
    collections.abc.AsyncGenerator,
)
# The text changes.declared_return gives in place of annotations that raise on 3.14.
RAISING = "annotations that raise "
# The members a mistake names, so a large Enum gives a short line.
SHOWN_MEMBERS = 10
# The pydantic errors a mistake names.
SHOWN_ERRORS = 10


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
    # By identity: `in` would call the class's own __eq__, which a team's metaclass can make raise.
    if not isinstance(target, type) or any(target is plain for plain in PLAIN):
        return returns
    name = kind or f"{target.__module__}:{target.__qualname__}"
    if isinstance(target, enum.EnumMeta):  # an Enum class
        return member(cast("type[enum.Enum]", target), returns, name)
    failed: tuple[str, str] | None = None
    fields: dict[str, object] | None = None  # set when the class itself is called
    try:
        validate = getattr(target, "model_validate", None)
        if callable(validate):
            return validate(returns)
        if table(returns):
            fields = cast("dict[str, object]", returns)
            return target(**fields)
    except ScrimmageError:  # Scrimmage's own: a refused or stopped call in the team's code
        raise
    except Exception as exc:  # the team's own code: its validation, a hook or __post_init__
        keys = (
            unbound(target, fields) if isinstance(exc, TypeError) and fields is not None else None
        )
        failed = (type(exc).__name__, keys or failure(exc))
    if failed is not None:
        # Outside the except block, so the mistake holds no reference to the error: its
        # message can quote a value. The text names keys and error kinds, never a value.
        cause, why = failed
        raise ScrimmageMockError(f"the mock does not fit {name}: {why}", cause=cause)
    if is_instance(returns, target, name):
        return returns
    raise ScrimmageMockError(
        f"the mock does not fit {name}: expected a table of its fields; a value no table "
        "can build is built by a function named with returns_from"
    )


def signature_of(tool: Callable[..., object]) -> inspect.Signature:
    """The tool's signature, for binding its arguments. On Python 3.14 annotations are lazy,
    so each is kept as written text and never evaluated: one may name a type imported only
    for type checking, or raise."""
    if sys.version_info >= (3, 14):
        import annotationlib

        written = annotationlib.Format.STRING
        return inspect.signature(tool, annotation_format=written)  # pyright: ignore
    return inspect.signature(tool)


def unbound(target: type, fields: dict[str, object]) -> str | None:
    """Why the table's keys do not fit the class's parameters, or None. Read only once the
    class has raised TypeError, as a signature can be narrower than what the class takes,
    such as a decorated __init__'s. Binding's text names keys, never values."""
    try:
        signature = signature_of(target)
    except Exception:  # no signature, or the team's own class hook raised: say the class only
        return None
    try:
        signature.bind(**fields)
    except TypeError as exc:
        return str(exc)
    return None


def failure(exc: Exception) -> str:
    """What failed, without the error's message, which can quote the value: a pydantic
    error's fields and error types, or any other error's class."""
    pydantic_core = sys.modules.get("pydantic_core")
    if pydantic_core is not None and isinstance(exc, pydantic_core.ValidationError):
        try:
            read = cast("Callable[..., list[dict[str, object]]]", getattr(exc, "errors", None))
            errors = read(include_input=False, include_url=False)
            found = [f"{place(error['loc'])} ({error['type']})" for error in errors]
        except Exception:  # an error whose details cannot be read: its class still says it
            return f"building it raised {type(exc).__name__}"
        more = f", and {len(found) - SHOWN_ERRORS} more" if len(found) > SHOWN_ERRORS else ""
        return f"{type(exc).__name__} at {', '.join(found[:SHOWN_ERRORS])}{more}"
    return f"building it raised {type(exc).__name__}"


def place(loc: object) -> str:
    """A pydantic error's location as text: the field names and item indexes to it."""
    return ".".join(str(part) for part in cast("tuple[object, ...]", loc)) or "the whole value"


def is_instance(value: object, target: type, name: str) -> bool:
    """isinstance against the team's class, whose own __instancecheck__ may raise. That is a
    mistake in the team's class, named by its error's class only, raised outside the except
    block so it holds no reference to the error: its message can quote a value."""
    failed: str | None = None
    try:
        return isinstance(value, target)
    except Exception as exc:  # the team's own hook
        failed = type(exc).__name__
    raise ScrimmageMockError(
        f"the mock does not fit {name}: the class's isinstance check raised {failed}"
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


# How a fix asks for a value of each plain kind, as TOML writes it.
WRITTEN = {
    str: "text",
    bool: "true or false",
    int: "an integer",
    float: "a number",
    list: "a list",
    tuple: "a list",
    dict: "a table",
}


def mismatch(
    value: object, kind: str | None, declared: object, name: str, factory: str | None = None
) -> str | None:
    """What is wrong with a built mock value, against the tool's declared return type, and
    the fix; None when it fits, or when the declared type tells nothing. value is the mock
    as built, before a framework shapes it; kind is the mock's type, factory its returns_from.
    Only the outer type is checked, and, for a class item type, the first item."""
    shown = type_text(declared)
    if fits(value, declared) is False:
        if factory is not None:
            return (
                f"returns_from {factory} gives {a_kind(value)}, but {name} is declared to "
                f"return {shown}; fix the function, or the annotation"
            )
        if kind is not None:
            return (
                f"builds {kind}, but {name} is declared to return {shown}; fix the mock's "
                "type, or the annotation"
            )
        return (
            f"returns {written_as(value)}, but {name} is declared to return {shown}; "
            f"{fix_for(value, declared)}"
        )
    item = item_type(declared)
    first = first_item(value)
    if factory is None and item is not None and table(first) and fits(first, item) is False:
        return (
            f"keeps its items as tables, but {name} is declared to return {shown}; "
            "use returns_from to build them, or fix the annotation"
        )
    return None


def unresolved(
    value: object, kind: str | None, declared: object, name: str, factory: str | None = None
) -> str | None:
    """The note for a table kept as written because the return type cannot be resolved. A
    returns_from mock builds its own value, and type is refused beside it: nothing to say."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    if kind is not None or factory is not None:
        return None
    if not isinstance(declared, changes.Unresolved) or not table(value):
        return None
    if declared.text.startswith(RAISING):  # on 3.14: not the annotation's text, so not quoted
        error = declared.text.removeprefix(RAISING)
        what = f"declares a return annotation that raises {error}"
        last = "Class"
    else:
        what = f"declares a return type Scrimmage cannot resolve ({declared.text!r})"
        last = declared.text.strip("'\"").rpartition(".")[2]
        last = last if last.isidentifier() else "Class"
    return (
        f'{name} {what}, so the mock is kept as a table; name the class with type = "module:{last}"'
    )


def fits(value: object, declared: object) -> bool | None:
    """Whether a value fits a declared type: None when the type tells nothing, such as none,
    Any, object, None alone (TOML cannot write None), a type variable, Literal, a NewType, a
    Protocol, an iterator or generator, or an unresolved type. An int fits float, a bool
    fits int, a bytearray fits bytes, and a list fits tuple, as TOML has no tuple."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    declared = plain_of(declared)
    if typing.get_origin(declared) in (typing.Union, types.UnionType):
        found = [
            value is None if member is types.NoneType else fits(value, member)
            for member in changes.members(declared)
        ]
        return True if True in found else None if None in found else False
    if any(declared is said for said in (None, types.NoneType, typing.Any, object)):
        return None
    if isinstance(declared, typing.TypeVar | typing.NewType | changes.Unresolved):
        return None
    origin = typing.get_origin(declared) or declared
    if origin is typing.Literal or not isinstance(origin, type) or origin in STREAMS:
        return None
    if typed_dict(origin):
        return isinstance(value, dict)  # isinstance refuses a TypedDict
    if getattr(origin, "_is_protocol", False):
        return None
    if (origin is float and type(value) is int) or (origin is tuple and isinstance(value, list)):
        return True
    if origin is bytes and isinstance(value, bytearray | memoryview):  # as typing once promoted
        return True
    try:
        return isinstance(value, origin)
    except Exception:  # a class whose own instance check raises tells nothing
        return None


def typed_dict(kind: type) -> bool:
    """A TypedDict, made by typing or by typing_extensions, whose classes typing does not
    know: both are dict subclasses with the keys they require."""
    return typing.is_typeddict(kind) or (
        dict in getattr(kind, "__mro__", ()) and hasattr(kind, "__required_keys__")
    )


def plain_of(declared: object) -> object:
    """Annotated[X, ...] read as X."""
    while typing.get_origin(declared) is typing.Annotated:
        declared = typing.get_args(declared)[0]
    return declared


def item_type(declared: object) -> object:
    """The item type of list[X], tuple[X, ...] or dict[str, X] when X is a class that is no
    plain kind; else None."""
    declared = plain_of(declared)
    origin, args = typing.get_origin(declared), typing.get_args(declared)
    homogeneous = origin is tuple and len(args) == 2 and args[1] is Ellipsis
    if (origin is list and len(args) == 1) or homogeneous:
        item = args[0]
    elif origin is dict and len(args) == 2:
        item = args[1]
    else:
        return None
    item = plain_of(item)
    return item if isinstance(item, type) and item not in PLAIN else None


def first_item(value: object) -> object:
    if isinstance(value, list) and value:
        return cast(list[object], value)[0]
    if isinstance(value, dict) and value:
        return next(iter(cast(dict[object, object], value).values()))
    return None


def a_kind(value: object) -> str:
    """A value by its class, as a returns_from function gave it."""
    return "None" if value is None else f"a {type(value).__qualname__}"


def written_as(value: object) -> str:
    """A TOML value as the team wrote it."""
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "text"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    return a_kind(value)


def fix_for(value: object, declared: object) -> str:
    """The fix for a written value that does not fit: write it as the declared kind, name
    the class to build with type, or build it with returns_from."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    ways: list[str] = []
    classes = False
    for member in changes.members(plain_of(declared)):
        origin = plain_of(member)
        origin = typing.get_origin(origin) or origin
        if isinstance(origin, type) and typed_dict(origin):
            origin = dict
        if origin in WRITTEN:
            ways.append(WRITTEN[origin])
        elif origin is not types.NoneType:
            classes = True
    ways = list(dict.fromkeys(ways))
    if ways:
        return f"write returns as {' or '.join(ways)}, or fix the annotation"
    if classes and table(value):
        return "name the class to build with type, or fix the annotation"
    if classes:
        return "build the value with returns_from, or fix the annotation"
    return "fix the mock, or the annotation"


def type_text(declared: object) -> str:
    """A declared type as a person writes it: Stock, list[Order], Stock | None."""
    from scrimmage_core import changes  # changes imports config, which imports this module

    declared = plain_of(declared)
    if typing.get_origin(declared) in (typing.Union, types.UnionType):
        return " | ".join(type_text(member) for member in changes.members(declared))
    if declared is types.NoneType or declared is None:
        return "None"
    if declared is Ellipsis:
        return "..."
    origin, args = typing.get_origin(declared), typing.get_args(declared)
    if origin is not None and args and isinstance(origin, type):
        return f"{origin.__qualname__}[{', '.join(type_text(arg) for arg in args)}]"
    if isinstance(declared, type):
        return declared.__qualname__
    return str(declared).removeprefix("typing.")


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
            f"the mock does not fit {name}: the value is not one of {shown}"
        ) from None
