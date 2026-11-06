"""Labels name a call, as tool:<name>. This version knows only tool labels."""

import re

from faultline_sdk.errors import ConfigError

# The name the model sees. Providers allow hyphens, which a Python function cannot have.
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")


def check(text: str, where: str, *, pattern: bool = False) -> str:
    """Check a label and return it. pattern allows tool:* (only under real)."""
    kind, sep, name = text.partition(":")
    if kind != "tool" or not sep or not name:
        raise ConfigError(
            f"{where}: {text!r} is not a label of the form 'tool:<name>'; see docs/configuration.md"
        )
    if name == "*" and not pattern:
        raise ConfigError(f"{where}: 'tool:*' is allowed only under [agent] real")
    if name != "*" and not TOOL_NAME.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} in {text!r} is not a tool name")
    return text


def matches_real(real: list[str], label: str) -> bool:
    """Whether a real entry, tool:<name> or tool:*, covers this label."""
    kind = label.partition(":")[0]
    return any(entry == label or entry == f"{kind}:*" for entry in real)
