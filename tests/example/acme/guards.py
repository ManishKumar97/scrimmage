"""Acme's hand-written output checks, which the shop bot calls on its draft answer.

They stand in for a team's own deny-list check. Each reads only its argument.
"""

DENIED = ("canary",)  # words the bot must never say; compared without case


class Blocked(Exception):
    """raising_check's way to say an answer may not go out."""


def check_answer(text: str) -> str:
    """Says "allow" or "block"."""
    return "block" if any(word in text.casefold() for word in DENIED) else "allow"


def raising_check(text: str) -> None:
    """The same check, for a guard that says no by raising, as some libraries do."""
    if check_answer(text) == "block":
        raise Blocked("the answer holds a denied word")


def strip_notes(text: str) -> str:
    """A filter on tool results: it drops every line that addresses the assistant and lets the
    rest through. It never blocks."""
    kept = [line for line in text.splitlines() if "note to the assistant" not in line.casefold()]
    return "\n".join(kept).strip()


# A check stored the moment it is defined keeps the original function, as a registry would.
CHECKS = {"answer": check_answer}
