"""Say when a web request goes around the gate.

Scrimmage gates httpx and httpx2. A request sent with requests, urllib3, urllib.request or
aiohttp runs for real and leaves no line. This module watches the one send path those clients
share and writes a notice for each distinct request, naming the library, the host and the
method. It never changes a call, and it never raises into the agent.

A client is watched when it sends, not when it is imported: importing openai or langsmith
loads these libraries without sending anything through them.
"""

import contextlib
import functools
import sys
import threading
import weakref
from collections.abc import Generator
from datetime import UTC, datetime
from types import ModuleType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from scrimmage import hooks
from scrimmage.errors import docs_url
from scrimmage.redact import redact

if TYPE_CHECKING:
    from scrimmage.state import ProcessState, RunState

KIND = "ungated"
MARK = "__scrimmage_ungated__"
# The libraries a request is named for, as a module name starts. The outermost wins: requests
# sits on urllib3, which sits on http.client.
LIBRARIES = ("requests", "urllib3", "urllib.request", "aiohttp")
FALLBACK = "http.client"
MAX_FRAMES = 200  # how far up the stack the library is looked for
READS = ("GET", "HEAD")
WHERE = docs_url("limits/calls") + "#web-requests-and-recorded-mocks"

_own = threading.local()


@contextlib.contextmanager
def own() -> Generator[None]:
    """Scrimmage's own connection is in use in this thread, so the observer skips it."""
    before = getattr(_own, "on", False)
    _own.on = True
    try:
        yield
    finally:
        _own.on = before


def library_of() -> str:
    """The outermost watched library on this thread's stack, else http.client."""
    found = FALLBACK
    try:
        frame = sys._getframe(1)  # pyright: ignore[reportPrivateUsage]
    except ValueError:
        return found
    seen = 0
    while frame is not None and seen < MAX_FRAMES:
        name = str(frame.f_globals.get("__name__", ""))
        for library in LIBRARIES:
            if name == library or name.startswith(library + "."):
                found = library
        frame = frame.f_back
        seen += 1
    return found


def message(library: str, host: str, method: str) -> str:
    result = "it was not gated" if method in READS else "it ran for real"
    return (
        f"{library} sent a {method} request to {host}; Scrimmage does not gate {library}, so "
        f"{result}. See {WHERE}"
    )


class Observer:
    """Writes one notice for each distinct (library, host, method) of a run."""

    def __init__(self, process: "ProcessState") -> None:
        self.process = process
        self.lock = threading.Lock()
        self.seen: weakref.WeakKeyDictionary[RunState, set[tuple[str, str, str]]] = (
            weakref.WeakKeyDictionary()
        )
        self.outside: set[tuple[str, str, str]] = set()  # a request with no run to hold it

    def observe(self, library: str, host: str, method: str) -> None:
        """Only the three names are kept: never a path, a query, a header or a body."""
        try:
            host = redact(host.rpartition("@")[2])  # never a login, never a key in a name
            key = (library, host, method)
            run = self.process.current()
            with self.lock:
                held = self.outside if run is None else self.seen.setdefault(run, set())
                if key in held:
                    return
                held.add(key)
            text = message(library, host, method)
            if run is None:
                self.process.notice(KIND, library, text)
                return
            now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            run.notice(KIND, library, text, extra={"host": host, "method": method, "start": now})
        except Exception:  # watching must never break the agent's request
            return


def patch_http_client(module: ModuleType, observer: Observer) -> None:
    """HTTPConnection.putrequest: the call http.client, urllib3 and so requests all make."""
    cls = getattr(module, "HTTPConnection", None)
    original = getattr(cls, "putrequest", None)
    if cls is None or original is None or getattr(original, MARK, False):
        return

    @functools.wraps(original)
    def putrequest(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not getattr(_own, "on", False):
            with contextlib.suppress(Exception):
                method = str(args[0] if args else kwargs.get("method", ""))
                observer.observe(library_of(), target_of(self, args, kwargs), method.upper())
        return original(self, *args, **kwargs)

    setattr(putrequest, MARK, True)
    cls.putrequest = putrequest


def target_of(conn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The host the request is for: behind a proxy, the target and not the proxy. A tunnel
    (HTTPS through CONNECT) names it, and so does the absolute URL a forwarding proxy gets."""
    tunnel = getattr(conn, "_tunnel_host", None)
    if tunnel:
        return str(tunnel)
    url = str(args[1] if len(args) > 1 else kwargs.get("url", ""))
    if url.startswith(("http://", "https://")):
        return urlsplit(url).hostname or ""
    return str(getattr(conn, "host", ""))


def host_of(session: Any, url: object) -> str:
    host = urlsplit(str(url)).hostname
    if host:
        return host
    base = getattr(session, "_base_url", None)  # a relative URL joins this one
    return "" if base is None else urlsplit(str(base)).hostname or ""


def patch_aiohttp(module: ModuleType, observer: Observer) -> None:
    """ClientSession._request: the one coroutine every aiohttp request goes through."""
    cls = getattr(module, "ClientSession", None)
    original = getattr(cls, "_request", None)
    if cls is None or original is None or getattr(original, MARK, False):
        return

    @functools.wraps(original)
    async def request(self: Any, method: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        with contextlib.suppress(Exception):
            observer.observe("aiohttp", host_of(self, url), str(method).upper())
        return await original(self, method, url, *args, **kwargs)

    setattr(request, MARK, True)
    cls._request = request


def install(process: "ProcessState") -> Observer:
    observer = Observer(process)
    hooks.on_import("http.client", lambda module: patch_http_client(module, observer))
    hooks.on_import("aiohttp.client", lambda module: patch_aiohttp(module, observer))
    return observer
