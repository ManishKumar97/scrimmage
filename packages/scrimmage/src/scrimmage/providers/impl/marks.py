"""How a provider tells whether a request was built from a faulted reply.

The one place the check's arithmetic lives, for every provider: the messages of a request are
normalised the way a client writes them again, hashed prefix by prefix in one pass, and an id a
request sends is looked up among the ids a fault gave. A provider decides what a message is and
what an id is; this decides how they are compared. The import contract lets only the providers'
table modules import it."""

import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import cast


def among(found: object, ids: frozenset[str]) -> bool:
    """Whether an id a request sends is one of these. A list or an object as an id cannot be
    looked up in a set, and is never one of them."""
    return isinstance(found, str) and found in ids


def digest(prefix: list[object]) -> tuple[int, bytes]:
    """The count and digest of these messages, as a request that starts with them gets."""
    *_, last = running(prefix, {len(prefix)})
    return last


def running(messages: Iterable[object], counts: set[int]) -> Iterator[tuple[int, bytes]]:
    """For each count in counts, the count and the sha256 of the canonical JSON of the list of
    the first that many messages. One hash is built message by message, so a long request is
    read once. A key order or a space does not change it; a value does."""
    whole = hashlib.sha256(b"[")
    for count, message in enumerate(messages, 1):
        if count > 1:
            whole.update(b",")
        whole.update(json.dumps(message, sort_keys=True, separators=(",", ":")).encode())
        if count in counts:
            part = whole.copy()
            part.update(b"]")
            yield count, part.digest()


def normalised(value: object) -> object:
    """A message as a later request may write it again: None, empty and missing are alike, as a
    client that drops None fields sends them."""
    if isinstance(value, dict):
        kept = {str(k): normalised(v) for k, v in cast("dict[object, object]", value).items()}
        return {k: v for k, v in kept.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [normalised(item) for item in cast("list[object]", value)]
    return value


TEXT_PARTS = ("text", "input_text")


def keyed(value: object) -> object:
    """A message as it is compared to tell one conversation from another, when its turns are
    written in different forms: no cache_control, which moves between turns; a string content
    and the one text part that says the same alike; an input_text part and a text part alike; and
    a type of message left out. Then as normalised."""
    if isinstance(value, dict):
        held = {str(k): v for k, v in cast("dict[object, object]", value).items()}
        held.pop("cache_control", None)
        if held.get("type") == "message":
            held.pop("type")
        if held.get("type") in TEXT_PARTS:
            held["type"] = "text"
        if isinstance(held.get("content"), str):
            held["content"] = [{"type": "text", "text": held["content"]}]
        return normalised({k: keyed(v) for k, v in held.items()})
    if isinstance(value, list):
        return normalised([keyed(item) for item in cast("list[object]", value)])
    return value
