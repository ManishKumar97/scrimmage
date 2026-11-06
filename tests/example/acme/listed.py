"""Tools named under [agent] tools, one of which skips the gate."""

from typing import Any

from acme.graph import get_stock
from acme.unsafe import OverridesInvoke, offer

__all__ = ["answer", "get_stock", "write_order"]


class WriteOrder(OverridesInvoke):
    name: str = "write_order"


write_order = WriteOrder()


def answer(question: str) -> dict[str, Any]:
    return offer(write_order, question)
