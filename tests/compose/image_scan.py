"""Search a `docker save` archive for secrets, layer by layer.

Docker 29 with the containerd snapshotter writes OCI layout with gzip layer blobs, so a
raw grep over the archive sees nothing. The scan is driven by the image's own lists:
`index.json` and the manifests it names (OCI), or `manifest.json` (classic). Every layer
they list must open with tarfile "r:*" (plain tar or gzip) and every regular file in it is
searched. Every other file in the archive is searched as bytes, and one that starts with a
compression magic (gzip, bzip2, xz, zstd, a zstd skippable frame) is an error, because its
bytes would not be searched. A layer it cannot read is an error, never a skip.
Limit: archives nested inside a layer, such as a .tar.gz file, are not opened.
"""

import json
import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

PEM_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{40,}")
_MAGICS = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")
_READ_ERRORS = (tarfile.TarError, EOFError, OSError, zlib.error)


class UnreadableLayer(Exception):
    """A layer, or a compressed blob, that the scan cannot search."""


@dataclass(frozen=True)
class Hit:
    layer: str
    file: str
    what: str


def _compressed(data: bytes) -> bool:
    if data.startswith(_MAGICS):
        return True
    # zstd skippable frames: 0x184D2A50 to 0x184D2A5F, little endian.
    return len(data) >= 4 and 0x50 <= data[0] <= 0x5F and data[1:4] == b"\x2a\x4d\x18"


def _search(layer: str, name: str, data: bytes, canaries: list[bytes]) -> list[Hit]:
    hits = [Hit(layer, name, f"canary {c.decode()!r}") for c in canaries if c in data]
    if PEM_PRIVATE_KEY.search(data):
        hits.append(Hit(layer, name, "pem private key"))
    return hits


def _scan_layer(name: str, handle: IO[bytes], canaries: list[bytes]) -> list[Hit]:
    hits: list[Hit] = []
    try:
        with tarfile.open(fileobj=handle, mode="r:*") as blob:
            for member in blob:
                if not member.isfile():
                    continue
                inner = blob.extractfile(member)
                if inner is not None:
                    hits += _search(name, member.name, inner.read(), canaries)
    except _READ_ERRORS as exc:
        raise UnreadableLayer(f"{name}: {exc}") from exc
    return hits


def _blob_path(digest: str) -> str:
    algorithm, _, value = digest.partition(":")
    return f"blobs/{algorithm}/{value}"


def _read(outer: tarfile.TarFile, name: str) -> bytes | None:
    try:
        handle = outer.extractfile(name)
    except KeyError:
        return None
    return None if handle is None else handle.read()


def _json(outer: tarfile.TarFile, name: str) -> Any:
    data = _read(outer, name)
    if data is None:
        raise UnreadableLayer(f"{name}: missing")
    try:
        return json.loads(data)
    except ValueError as exc:
        raise UnreadableLayer(f"{name}: not JSON") from exc


def _oci_layers(outer: tarfile.TarFile, descriptors: list[Any], seen: set[str]) -> set[str]:
    layers: set[str] = set()
    for desc in descriptors:
        path = _blob_path(str(desc.get("digest", "")))
        if path in seen:
            continue
        seen.add(path)
        doc = _json(outer, path)
        if "manifests" in doc:
            layers |= _oci_layers(outer, doc["manifests"], seen)
        for layer in doc.get("layers", []):
            # Attestation "layers" (in-toto JSON) are not tars; they are searched as bytes.
            kind = str(layer.get("mediaType", "layer"))
            if "layer" in kind or "diff" in kind:
                layers.add(_blob_path(str(layer.get("digest", ""))))
    return layers


def _layer_paths(outer: tarfile.TarFile) -> set[str]:
    layers: set[str] = set()
    found = False
    if _read(outer, "index.json") is not None:
        found = True
        layers |= _oci_layers(outer, _json(outer, "index.json").get("manifests", []), set())
    if _read(outer, "manifest.json") is not None:
        found = True
        for entry in _json(outer, "manifest.json"):
            layers |= {str(p) for p in entry.get("Layers", [])}
    if not found:
        raise UnreadableLayer("no index.json or manifest.json: cannot tell which files are layers")
    return layers


def scan_image(archive: Path, canaries: list[str]) -> list[Hit]:
    """Return every canary or PEM private key found in any file of the archive."""
    needles = [c.encode() for c in canaries]
    if any(not n for n in needles):
        raise ValueError("an empty canary matches everything")
    hits: list[Hit] = []
    with tarfile.open(archive, "r:*") as outer:
        layers = _layer_paths(outer)
        names = {m.name for m in outer if m.isfile()}
        for missing in sorted(layers - names):
            raise UnreadableLayer(f"{missing}: listed by the image but not in the archive")
        for member in outer:
            if not member.isfile():
                continue
            handle = outer.extractfile(member)
            if handle is None:
                continue
            if member.name in layers:
                hits += _scan_layer(member.name, handle, needles)
                continue
            data = handle.read()
            if _compressed(data):
                raise UnreadableLayer(f"{member.name}: compressed data outside the layer list")
            hits += _search("-", member.name, data, needles)
    return hits
