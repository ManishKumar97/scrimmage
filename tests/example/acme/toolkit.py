"""Acme's tools as methods on a class, the way agents such as those on τ²-bench write them.

List them as acme.toolkit:StoreTools.get_order and so on. get_order comes from the base
class, check_inventory is a staticmethod and refund_order a classmethod. The instance
holds a connection string, which must never reach a record.
"""

import json
import os

import openai

from acme import agent
from acme.models import Stock

# A long, fixed policy, as a support agent has, so a real provider caches the prompt.
POLICY = "You are Acme's order assistant. Follow the policy below.\n" + "\n".join(
    f"Policy {n}: when a customer asks about an order, look it up with get_order before "
    f"you answer, quote the status and the date exactly as the tool gives them, and never "
    f"promise a refund unless the refund tool says it went through. Rule set {n} of 25."
    for n in range(1, 26)
)


class OrderTools:
    def __init__(self, database: str) -> None:
        self.database = database

    def get_order(self, order_id: str) -> str:
        raise RuntimeError(f"the real orders database at {self.database} is not in tests")


class StoreTools(OrderTools):
    @staticmethod
    def check_inventory(sku: str) -> str:
        return f"{sku}: 12 in stock"

    @classmethod
    def refund_order(cls, order_id: str) -> str:
        raise RuntimeError(f"a real refund of {order_id} must never run in a test")

    def get_stock(self, sku: str) -> Stock:
        raise RuntimeError("the real stock service is not reachable from tests")


def answer(question: str) -> str:
    toolkit = StoreTools("postgres://acme:s3cret@orders.acme.internal/orders")

    def run(name: str, arguments: str) -> str:
        try:
            result = getattr(toolkit, name)(**json.loads(arguments))
        except Exception as exc:  # the bot tells the model what went wrong
            return f"error: {type(exc).__name__}: {exc}"
        if isinstance(result, Stock):
            return f"{result.sku}: {result.count} in stock"
        return str(result)

    model = os.environ.get("ACME_MODEL", "gpt-4o-mini")
    return agent.loop(openai.OpenAI(max_retries=0), question, run, POLICY, model)
