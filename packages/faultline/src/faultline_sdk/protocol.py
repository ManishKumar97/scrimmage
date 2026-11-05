"""Signing between the SDK and the control server.

The token never travels. A request is signed over its method, path, time, a one-time
nonce and its body's hash. A reply is signed over the request's own signature, its
status and its body's hash, so a reply can answer only the request it was made for.
"""

import hashlib
import hmac
import secrets
import threading
import time

TIME = "X-Faultline-Time"
NONCE = "X-Faultline-Nonce"
SIGNATURE = "X-Faultline-Signature"
WINDOW_S = 60.0  # how far a request's time may be from the server's clock
NONCE_KEEP_S = 2 * WINDOW_S


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _mac(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def sign_request(token: str, method: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    return _mac(token, "faultline-request-v1", method, path, stamp, nonce, digest(body))


def sign_reply(token: str, request_signature: str, status: int, body: bytes) -> str:
    return _mac(token, "faultline-reply-v1", request_signature, str(status), digest(body))


def request_headers(token: str, method: str, path: str, body: bytes, now: float) -> dict[str, str]:
    stamp = f"{now:.3f}"
    nonce = secrets.token_hex(16)
    return {
        TIME: stamp,
        NONCE: nonce,
        SIGNATURE: sign_request(token, method, path, stamp, nonce, body),
    }


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class NonceStore:
    """Nonces seen in the last two minutes. A nonce seen before is a replay."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_use(self, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            for old in [n for n, t in self._seen.items() if now - t > NONCE_KEEP_S]:
                del self._seen[old]
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def check_request(
    token: str,
    nonces: NonceStore,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    now: float,
) -> str | None:
    """None if the request is good, else the error code to send back."""
    stamp, nonce, signature = (
        headers.get(TIME),
        headers.get(NONCE),
        headers.get(SIGNATURE),
    )
    if not stamp or not nonce or not signature or len(nonce) != 32:
        return "bad_signature"
    if not same(signature, sign_request(token, method, path, stamp, nonce, body)):
        return "bad_signature"
    try:
        sent = float(stamp)
    except ValueError:
        return "bad_signature"
    if abs(now - sent) > WINDOW_S:
        return "clock"
    if not nonces.first_use(nonce, now):
        return "replayed"
    return None
