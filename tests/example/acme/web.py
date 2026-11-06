"""Acme's tools that call other services over the web, with httpx.

The bot's own error handling lives here, so a fault on the web request tests it.
"""

import os

import httpx

BASE = os.environ.get("INVENTORY_URL", "https://inventory.acme.internal")


def check_inventory(sku: str) -> str:
    try:
        reply = httpx.get(f"{BASE}/stock/{sku}", timeout=2)
        reply.raise_for_status()
    except httpx.HTTPError as exc:
        return f"inventory unavailable: {type(exc).__name__}"
    return f"{reply.json()['count']} in stock"


def refund(order_id: str) -> str:
    reply = httpx.post("https://payments.acme.internal/refunds", json={"order": order_id})
    return reply.text


def report(sku: str) -> str:
    """Sends a key in its headers and its query, which must never reach a record."""
    reply = httpx.get(
        f"{BASE}/stock/{sku}",
        params={"api_key": "query-secret-value"},
        headers={"Authorization": "header-secret-value", "Cookie": "session=cookie-secret-value"},
    )
    return reply.text


def answer(question: str) -> str:
    return check_inventory("SKU-42")


def answer_refund(question: str) -> str:
    return refund("1234")


def answer_report(question: str) -> str:
    return report("SKU-42")


def answer_two_paths(question: str) -> str:
    return httpx.get(f"{BASE}/stock/SKU-42").text + httpx.get(f"{BASE}/stock/SKU-99").text
