"""Turn a mock's TOML value into what the tool really returns.

A value is turned into the tool's own type when it has one: the mock's type, or the
tool's return annotation when type is left out. A pydantic model is built with
model_validate, any other class with Type(**returns). A value that does not fit is a
mistake in the test.
"""

import importlib
import inspect
import typing
from collections.abc import Callable

from derail_sdk.errors import DerailMockError, DerailRefused

# Any is a class on 3.11 and later, and neither it nor object names a shape to build.
PLAIN = (str, int, float, bool, list, dict, tuple, type(None), object, typing.Any)


def allowed(path: str, prefixes: list[str]) -> bool:
    module = path.partition(":")[0]
    return any((module + ".").startswith(prefix) for prefix in prefixes)


def import_object(path: str, prefixes: list[str]) -> object:
    """Import module:attr, only under an allowed prefix, checked before importing."""
    if not allowed(path, prefixes):
        raise DerailRefused(
            f"{path} is outside DERAIL_ALLOW_IMPORTS ({', '.join(prefixes) or 'empty'}), "
            "so it is not imported"
        )
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def return_type(tool: Callable[..., object]) -> object:
    try:
        return typing.get_type_hints(tool).get("return")
    except Exception:  # a forward reference that cannot be resolved
        annotation = inspect.signature(tool).return_annotation
        return None if isinstance(annotation, str) else annotation


def build(
    returns: object, kind: str | None, tool: Callable[..., object], prefixes: list[str]
) -> object:
    target = import_object(kind, prefixes) if kind else return_type(tool)
    if not isinstance(target, type) or target in PLAIN:
        return returns
    name = kind or f"{target.__module__}:{target.__qualname__}"
    try:
        validate = getattr(target, "model_validate", None)
        if callable(validate):
            return validate(returns)
        if isinstance(returns, dict):
            return target(**returns)
        if isinstance(returns, target):
            return returns
    except (TypeError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise DerailMockError(f"the mock does not fit {name}: {exc}") from exc
    raise DerailMockError(f"the mock does not fit {name}: expected a table of its fields")
