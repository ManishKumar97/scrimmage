"""Acme's tools that call other services over the web, with httpx.

The bot's own error handling lives here, so a fault on the web request tests it.
"""

import os

import httpx

BASE = os.environ.get("INVENTORY_URL", "https://inventory.acme.internal")
# In a test the service is a local server, which a loaded machine can keep for seconds. A
# timeout here would fail a run for the clock, so it is far longer than any answer takes.
TIMEOUT_S = 60


def check_inventory(sku: str) -> str:
    try:
        reply = httpx.get(f"{BASE}/stock/{sku}", timeout=TIMEOUT_S)
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


def answer_with_its_error(question: str) -> str:
    try:
        httpx.get(
            f"{BASE}/stock/SKU-42", params={"api_key": "query-secret-value"}
        ).raise_for_status()
    except httpx.HTTPError as exc:
        return str(exc)
    return "fine"


def answer_with_a_bad_header(question: str) -> str:
    try:
        httpx.get(f"{BASE}/stock/SKU-42", headers={"x-key": "header-secret-value\n"})
    except httpx.HTTPError as exc:
        return str(exc)
    return "fine"


def strict_lookup(sku: str) -> str:
    reply = httpx.get(f"{BASE}/stock/{sku}", params={"api_key": "query-secret-value"})
    reply.raise_for_status()
    return reply.text


def answer_through_a_strict_tool(question: str) -> str:
    try:
        return strict_lookup("SKU-42")
    except httpx.HTTPError:
        return "failed"


def answer_refund(question: str) -> str:
    return refund("1234")


def answer_report(question: str) -> str:
    return report("SKU-42")


def answer_two_paths(question: str) -> str:
    first = httpx.get(f"{BASE}/stock/SKU-42", timeout=TIMEOUT_S)
    return first.text + httpx.get(f"{BASE}/stock/SKU-99", timeout=TIMEOUT_S).text
