"""Decorators a team keeps in a module of its own, so a wrapper's globals are not the tool's."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any


def logged(function: Callable[..., Any]) -> Callable[..., Any]:
    """A function made by functools.wraps."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    return wrapper


def twice_logged(function: Callable[..., Any]) -> Callable[..., Any]:
    """A chain of two wrappers."""
    return logged(logged(function))


class Logged:
    """An object made a wrapper by functools.update_wrapper."""

    def __init__(self, function: Callable[..., Any]) -> None:
        functools.update_wrapper(self, function)
        self.function = function

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(*args, **kwargs)
