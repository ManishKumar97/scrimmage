"""Signing between the SDK and the control server.

The token never travels. A request is signed over its method, path, time, a one-time
nonce and its body's hash. A reply is signed over the request's own signature, its
status and its body's hash, so a reply can answer only the request it was made for.
"""

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections import deque

TIME = "X-Scrimmage-Time"
NONCE = "X-Scrimmage-Nonce"
SIGNATURE = "X-Scrimmage-Signature"
# The digest of the body the signature covers, sent so a server can check the signature
# before it reads a large body.
BODY_SHA256 = "X-Scrimmage-Body-Sha256"
MAX_BODY = 4 * 1024 * 1024  # a body over this is read only after its header is checked
WINDOW_S = 60.0  # how far a request's time may be from the server's clock
NONCE_KEEP_S = 2 * WINDOW_S
RUN_MAC_HEX = 16  # 64 bits of HMAC-SHA256 in a run header


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _mac(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def sign_digest(
    token: str, method: str, path: str, stamp: str, nonce: str, body_digest: str
) -> str:
    return _mac(token, "scrimmage-request-v1", method, path, stamp, nonce, body_digest)


def sign_request(token: str, method: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    return sign_digest(token, method, path, stamp, nonce, digest(body))


def sign_reply(token: str, request_signature: str, status: int, body: bytes) -> str:
    return _mac(token, "scrimmage-reply-v1", request_signature, str(status), digest(body))


def request_headers(token: str, method: str, path: str, body: bytes, now: float) -> dict[str, str]:
    stamp = f"{now:.3f}"
    nonce = secrets.token_hex(16)
    return {
        TIME: stamp,
        NONCE: nonce,
        SIGNATURE: sign_request(token, method, path, stamp, nonce, body),
        BODY_SHA256: digest(body),
    }


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def run_mac(token: str, name: str) -> str:
    """The MAC a run header carries for its run's name: only a token holder can make one."""
    return _mac(token, "scrimmage-run-name-v1", name)[:RUN_MAC_HEX]


# What Plan.to_json() adds beside the plan: the run's age, which changes with time, and the
# values ${NAME} filled in, which a digest on a volume must not let a reader test guesses at.
UNDIGESTED = ("elapsed_s", "scrub")


def plan_digest(token: str, plan: dict[str, object]) -> str:
    """HMAC-SHA256, keyed by the token, of a plan's to_json() without elapsed_s and scrub.

    Keyed, because the plan holds mock values and other text from the test file: a plain
    hash stored in a record would let anyone who reads it test a guess offline."""
    kept = {key: value for key, value in plan.items() if key not in UNDIGESTED}
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(token.encode(), text.encode(), hashlib.sha256).hexdigest()


class NonceStore:
    """Nonces seen in the last two minutes. A nonce seen before is a replay.

    An expiry queue ordered by time: each call drops only the entries that expired, then
    looks once, so its cost does not grow with the number stored. A service's probe exposes
    one of these to anyone who can reach its port."""

    def __init__(self) -> None:
        self._order: deque[tuple[float, str]] = deque()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def first_use(self, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            while self._order and now - self._order[0][0] > NONCE_KEEP_S:
                self._seen.discard(self._order.popleft()[1])
            if nonce in self._seen:
                return False
            self._seen.add(nonce)
            self._order.append((now, nonce))
            return True


def signed_head(
    token: str, method: str, path: str, headers: dict[str, str], now: float
) -> str | None:
    """None if the headers carry a fresh signature made with the token over the body digest
    they declare, else the error code to send back. It reads no body and uses no nonce: the
    full check follows once the body is in, and finds a body that is not the declared one.
    The time is checked here too, since a header captured earlier carries a good signature,
    and would otherwise make the server read a large body for anyone who replays it."""
    stamp, nonce, signature, declared = (
        headers.get(TIME),
        headers.get(NONCE),
        headers.get(SIGNATURE),
        headers.get(BODY_SHA256),
    )
    if not stamp or not nonce or not signature or not declared or len(nonce) != 32:
        return "bad_signature"
    if not same(signature, sign_digest(token, method, path, stamp, nonce, declared)):
        return "bad_signature"
    try:
        sent = float(stamp)
    except ValueError:
        return "bad_signature"
    if not math.isfinite(sent):
        return "bad_signature"
    if abs(now - sent) > WINDOW_S:
        return "clock"
    return None


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
    if not math.isfinite(sent):
        return "bad_signature"
    if abs(now - sent) > WINDOW_S:
        return "clock"
    if not nonces.first_use(nonce, now):
        return "replayed"
    return None
