"""Tools named under [agent] tools, one of which skips the gate."""

from typing import Any

from langchain_openai import ChatOpenAI

from acme.graph import get_stock
from acme.unsafe import OverridesInvoke, offer

__all__ = ["answer", "answer_other", "get_stock", "other_tool", "write_order"]


class WriteOrder(OverridesInvoke):
    name: str = "write_order"


write_order = WriteOrder()


class OtherFramework:
    """Shaped like a framework's tool, but no adapter ever sees it."""

    def invoke(self, tool_input: Any) -> str:
        return self._run(tool_input)

    def _run(self, tool_input: Any) -> str:
        return "REAL WRITE RAN"


other_tool = OtherFramework()


def answer_other(question: str) -> str:
    offered = {
        "type": "function",
        "function": {"name": "other_tool", "parameters": {"type": "object"}},
    }
    model = ChatOpenAI(model="gpt-4o-mini", max_retries=0).bind_tools([offered, get_stock])
    return str(model.invoke(question).content)


def answer(question: str) -> dict[str, Any]:
    return offer(write_order, question)
