"""Acme's tools. In a test they are mocked, faulted, run for real or refused."""

import os
import socket

from acme.models import Stock


def get_order(order_id: str) -> str:
    """Asks the orders system at ACME_ORDERS (host:port), waiting at most half a second."""
    address = os.environ.get("ACME_ORDERS")
    if not address:
        raise RuntimeError("the real orders system is not reachable from tests")
    host, port = address.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=0.5) as conn:
        conn.sendall(f"GET /orders/{order_id}\n".encode())
        return conn.recv(1024).decode()  # raises TimeoutError if the system never answers


def check_inventory(sku: str) -> str:
    return f"{sku}: 12 in stock"


def refund_order(order_id: str) -> str:
    raise RuntimeError(f"a real refund of {order_id} must never run in a test")


def get_stock(sku: str) -> Stock:
    raise RuntimeError("the real stock service is not reachable from tests")
