"""Search a `docker save` archive for secrets, layer by layer.

Docker 29 with the containerd snapshotter writes OCI layout with gzip layer blobs, so a
raw grep over the archive sees nothing. This opens every blob with tarfile "r:*" (plain
and gzip) and searches every regular file's bytes. A blob it cannot read is an error,
never a skip.
"""

import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

PEM_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\n[A-Za-z0-9+/=]{40,}")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class UnreadableLayer(Exception):
    """A layer blob that tarfile cannot open, for example a zstd one."""


@dataclass(frozen=True)
class Hit:
    layer: str
    file: str
    what: str


def _search(layer: str, name: str, data: bytes, canaries: list[bytes]) -> list[Hit]:
    hits = [Hit(layer, name, f"canary {c.decode()!r}") for c in canaries if c in data]
    if PEM_PRIVATE_KEY.search(data):
        hits.append(Hit(layer, name, "pem private key"))
    return hits


def _scan_layer(layer: str, blob: tarfile.TarFile, canaries: list[bytes]) -> list[Hit]:
    hits: list[Hit] = []
    for member in blob:
        if not member.isfile():
            continue
        handle = blob.extractfile(member)
        if handle is not None:
            hits += _search(layer, member.name, handle.read(), canaries)
    return hits


def _open_layer(name: str, handle: IO[bytes]) -> tarfile.TarFile | None:
    """Open a blob as a tar (plain or gzip), or None when it is not a tar at all."""
    try:
        return tarfile.open(fileobj=handle, mode="r:*")
    except tarfile.ReadError:
        return None
    except (EOFError, OSError) as exc:
        raise UnreadableLayer(f"{name}: {exc}") from exc


def scan_image(archive: Path, canaries: list[str]) -> list[Hit]:
    """Return every canary or PEM private key found in any layer of the archive."""
    needles = [c.encode() for c in canaries]
    if any(not n for n in needles):
        raise ValueError("an empty canary matches everything")
    hits: list[Hit] = []
    with tarfile.open(archive, "r:*") as outer:
        for member in outer:
            if not member.isfile():
                continue
            handle = outer.extractfile(member)
            if handle is None:
                continue
            inner = _open_layer(member.name, handle)
            if inner is None:
                handle.seek(0)
                data = handle.read()
                if data[:4] == ZSTD_MAGIC:
                    raise UnreadableLayer(f"{member.name}: zstd layer")
                # A manifest or config, not a layer: searched as bytes.
                hits += _search("-", member.name, data, needles)
                continue
            with inner:
                try:
                    hits += _scan_layer(member.name, inner, needles)
                except (tarfile.TarError, EOFError, OSError) as exc:
                    raise UnreadableLayer(f"{member.name}: {exc}") from exc
    return hits
