"""Tools named under [agent] tools, one of which skips the gate."""

from typing import Any

from acme.graph import get_stock
from acme.unsafe import OverridesInvoke, offer

__all__ = ["answer", "get_order", "get_stock"]

get_order = OverridesInvoke()


def answer(question: str) -> dict[str, Any]:
    return offer(get_order, question)
