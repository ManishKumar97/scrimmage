"""scrimmage prices update: the one place Scrimmage reads from the network.

It runs only when a person types the command, never while a test runs. It lives apart from
prices.py so that the modules that read a finished test never reach a network library.
"""

import dataclasses
import http.client
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from scrimmage.errors import ConfigError
from scrimmage.outcomes import INFRA, MISTAKE, OK
from scrimmage.prices import RUNS, SHIPPED, PriceFile, Rates, parse, shipped, team

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
    out = {k: v for k, v in dataclasses.asdict(rates).items() if k != "tiers" and v is not None}
    for tier, tiered in rates.tiers:
        out.update({f"tiers.{tier}.{k}": v for k, v in flat(tiered).items()})
    return out


def fmt(rates: Rates) -> str:
    return ", ".join(f"{k} {v:g}" for k, v in flat(rates).items())


def diff(old: PriceFile, new: PriceFile) -> list[str]:
    """What an update changes: added, removed and changed rows, old rate to new."""
    before, after = dict(old.models), dict(new.models)
    lines = [f"added {m}: {fmt(r)}" for m, r in after.items() if m not in before]
    lines += [f"removed {m}: {fmt(r)}" for m, r in before.items() if m not in after]
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
                moves.append(f"{key} {a[key]:g} to {b[key]:g}")
            elif key in b:
                moves.append(f"{key} added at {b[key]:g}")
            else:
                moves.append(f"{key} removed, was {a[key]:g}")
        lines.append(f"changed {model}: {'; '.join(moves)}")
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


def fetch(url: str) -> str:
    """The one place Scrimmage reads from the network, called only by `scrimmage prices update`.

    The whole fetch has TIMEOUT_S seconds, and no single read waits longer than READ_S.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError(f"{url} is not an https address")
    context = ssl.create_default_context(cafile=CAFILE)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), SameHostHttps
    )
    deadline = time.monotonic() + TIMEOUT_S
    chunks: list[bytes] = []
    try:
        with opener.open(url, timeout=min(READ_S, TIMEOUT_S)) as reply:
            size = 0
            while chunk := reply.read1(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FetchError(f"{url} is larger than {MAX_BYTES} bytes")
                if time.monotonic() > deadline:
                    raise FetchError(f"{url} took longer than {TIMEOUT_S:g} seconds")
            promised = reply.headers.get("Content-Length")
            if promised is not None and promised.strip().isdigit() and int(promised) != size:
                raise FetchError(f"{url} sent {size} bytes of the {promised} it promised")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()  # an error reply holds a file until it is closed
        raise FetchError(f"cannot fetch {url}: {getattr(exc, 'reason', None) or exc!r}") from None
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
    print(f"compared with {was}, as of {old.as_of}; the new file is as of {new.as_of}")
    print("\n".join(diff(old, new)) or "no price changed")
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
