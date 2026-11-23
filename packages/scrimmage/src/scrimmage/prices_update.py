"""scrimmage prices update: the one place Scrimmage reads from the network.

It runs only when a person types the command, never while a test runs. It lives apart from
prices.py so that the modules that read a finished test never reach a network library.
"""

import contextlib
import dataclasses
import http.client
import os
import socket
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from scrimmage.errors import ConfigError
from scrimmage.outcomes import INFRA, MISTAKE, OK
from scrimmage.prices import RUNS, SHIPPED, PriceFile, Rates, Section, parse, shipped, team

# The one address Scrimmage ever fetches, and only when a person runs `scrimmage prices update`.
URL = (
    "https://raw.githubusercontent.com/ManishKumar97/scrimmage/main/"
    "packages/scrimmage/src/scrimmage/prices.toml"
)
CAFILE: str | None = None  # the authority to trust instead of the system's; a test sets it
TIMEOUT_S = 15.0  # the whole fetch
READ_S = 5.0  # one read of the socket
MAX_BYTES = 1_000_000


def flat(rates: Rates) -> dict[str, float]:
    skip = {"tiers", "long", "aliases", "fees", "models", "name"}
    out = {k: v for k, v in dataclasses.asdict(rates).items() if k not in skip and v is not None}
    out.update({f"fee.{k}": v for k, v in rates.fees})
    for tier, tiered in rates.tiers:
        out.update({f"tiers.{tier}.{k}": v for k, v in flat(tiered).items()})
    if rates.long is not None:
        out.update({f"long.{k}": v for k, v in flat(rates.long).items()})
    return out


def num(key: str, value: float) -> str:
    """A rate in short form; a threshold, `above`, as the whole number a team approves."""
    return str(int(value)) if key.rpartition(".")[2] == "above" else f"{value:g}"


def fmt(rates: Rates) -> str:
    return ", ".join(f"{k} {num(k, v)}" for k, v in flat(rates).items())


def diff(old: Section, new: Section) -> list[str]:
    """What an update changes in one provider's section: added, removed and changed rows, old
    rate to new."""
    before, after = dict(old.models), dict(new.models)
    lines = [f"added {m}: {fmt(r)}" for m, r in after.items() if m not in before]
    lines += [f"removed {m}: {fmt(r)}" for m, r in before.items() if m not in after]
    was_fees, now_fees = dict(old.fees), dict(new.fees)
    for fee in sorted(was_fees.keys() | now_fees.keys()):
        if was_fees.get(fee) == now_fees.get(fee):
            continue
        if fee in was_fees and fee in now_fees:
            lines.append(f"changed fee {fee}: {was_fees[fee]:g} to {now_fees[fee]:g}")
        elif fee in now_fees:
            lines.append(f"added fee {fee}: {now_fees[fee]:g}")
        else:
            lines.append(f"removed fee {fee}, was {was_fees[fee]:g}")
    for model, now in after.items():
        was = before.get(model)
        if was is None or was == now:
            continue
        a, b = flat(was), flat(now)
        moves: list[str] = []
        for key in sorted(a.keys() | b.keys()):
            if a.get(key) == b.get(key):
                continue
            if key in a and key in b:
                moves.append(f"{key} {num(key, a[key])} to {num(key, b[key])}")
            elif key in b:
                moves.append(f"{key} added at {num(key, b[key])}")
            else:
                moves.append(f"{key} removed, was {num(key, a[key])}")
        lines.append(f"changed {model}: {'; '.join(moves)}")
    return lines


def diff_files(old: PriceFile, new: PriceFile) -> list[str]:
    """What an update changes, grouped by provider: a heading for each section, with both
    dates, and under it the changes to its rows. A section only one file has is added or
    removed whole."""
    before, after = dict(old.sections), dict(new.sections)
    lines: list[str] = []
    for provider in [*after, *(p for p in before if p not in after)]:
        was, now = before.get(provider), after.get(provider)
        if now is None:
            lines.append(f"{provider}: section removed")
        elif was is None:
            lines.append(f"{provider}: section added, as of {now.as_of}")
            lines += [f"  added {m}: {fmt(r)}" for m, r in now.models]
        else:
            lines.append(f"{provider}: as of {was.as_of}, the new file is as of {now.as_of}")
            changes = diff(was, now)
            lines += [f"  {line}" for line in changes] or ["  no price changed"]
    return lines


class FetchError(Exception):
    """The file could not be fetched."""


class SameHostHttps(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on the same host. Any other raises before a request is
    made, so nothing is ever sent to another host or in plain text."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        here, there = urlsplit(req.full_url), urlsplit(newurl)
        if there.scheme != "https" or there.netloc != here.netloc:
            raise FetchError(
                f"the server tried to send the request on to {newurl}, so it was refused"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


class Watched(urllib.request.HTTPSHandler):
    """An HTTPS handler that hands every connection it opens to a list, so a timer can cut
    them at the deadline. A cut connection ends a read that is waiting, in the status line,
    a header or the body alike."""

    def __init__(
        self,
        context: ssl.SSLContext,
        opened: list[http.client.HTTPSConnection],
        expired: threading.Event,
    ) -> None:
        super().__init__(context=context)
        self.context, self.opened, self.expired = context, opened, expired

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        opened, expired = self.opened, self.expired

        class Connection(http.client.HTTPSConnection):
            def connect(self) -> None:
                super().connect()
                opened.append(self)
                if expired.is_set():  # the time went on the lookup and the handshake
                    raise FetchError(f"took longer than {TIMEOUT_S:g} seconds")

        return self.do_open(Connection, req, context=self.context)


def cut(opened: list[http.client.HTTPSConnection], expired: threading.Event) -> None:
    expired.set()
    for connection in list(opened):
        sock = connection.sock  # read once: the reading thread may close it
        if sock is not None:
            with contextlib.suppress(OSError):
                # The plain socket's shutdown, not the TLS wrapper's: it wakes a read that
                # is blocked in another thread without touching the TLS state under it.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)


def fetch(url: str) -> str:
    """The one place Scrimmage reads from the network, called only by `scrimmage prices update`.

    The whole exchange, connecting, the headers and the body, has TIMEOUT_S seconds: a timer
    cuts the connection when they are up. No single read waits longer than READ_S.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError(f"{url} is not an https address")
    context = ssl.create_default_context(cafile=CAFILE)
    opened: list[http.client.HTTPSConnection] = []
    expired = threading.Event()
    opener = urllib.request.build_opener(Watched(context, opened, expired), SameHostHttps)
    timer = threading.Timer(TIMEOUT_S, cut, (opened, expired))
    timer.daemon = True
    timer.start()
    chunks: list[bytes] = []
    try:
        with opener.open(url, timeout=min(READ_S, TIMEOUT_S)) as reply:
            size = 0
            while chunk := reply.read1(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FetchError(f"{url} is larger than {MAX_BYTES} bytes")
                if expired.is_set():  # a slow drip may keep every read short
                    raise FetchError(f"{url} took longer than {TIMEOUT_S:g} seconds")
            promised = reply.headers.get("Content-Length")
            if expired.is_set():
                raise FetchError(f"{url} took longer than {TIMEOUT_S:g} seconds")
            if promised is not None and promised.strip().isdigit() and int(promised) != size:
                raise FetchError(f"{url} sent {size} bytes of the {promised} it promised")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()  # an error reply holds a file until it is closed
        if expired.is_set():
            raise FetchError(f"{url} took longer than {TIMEOUT_S:g} seconds") from None
        raise FetchError(f"cannot fetch {url}: {getattr(exc, 'reason', None) or exc!r}") from None
    finally:
        timer.cancel()
        for connection in opened:
            connection.close()
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError(f"{url} is not UTF-8 text") from None


def update(root: Path, url: str | None = None) -> int:
    """scrimmage prices update. 0 written, 2 the file fails its schema, 3 no fetch.
    Nothing is written unless the file passed."""
    try:
        text = fetch(url or URL)
    except FetchError as exc:
        print(f"scrimmage prices update: {exc}; nothing was written", file=sys.stderr)
        return INFRA
    try:
        new = parse(text, "the fetched prices.toml")
    except ConfigError as exc:
        print(f"scrimmage prices update: {exc}; nothing was written", file=sys.stderr)
        return MISTAKE
    try:
        old, was = team(root), f"{RUNS}/{SHIPPED}"
    except ConfigError:  # a bad old copy is replaced, and the shipped file is what to compare
        old, was = None, ""
    if old is None:
        old, was = shipped(), f"the shipped {SHIPPED}"
    print(f"compared with {was}")
    print("\n".join(diff_files(old, new)))
    write(root, text)
    print(f"wrote {RUNS}/{SHIPPED}")
    return OK


def write(root: Path, text: str) -> None:
    """Write the team's copy into the runs folder, which only its owner may list and git
    ignores, as the runner makes it."""
    runs = root / RUNS
    runs.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runs, 0o700)
    ignore = runs / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    fd, name = tempfile.mkstemp(dir=runs, prefix=".prices-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(name, runs / SHIPPED)
