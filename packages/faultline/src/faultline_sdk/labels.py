"""Labels name a call, such as tool:get_order. This version knows only tool labels."""

from dataclasses import dataclass

from faultline_sdk.errors import ConfigError


@dataclass(frozen=True)
class Label:
    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


def parse(text: str, where: str, *, pattern: bool = False) -> Label:
    """Parse a label. pattern allows tool:* (only under real)."""
    kind, sep, name = text.partition(":")
    if kind != "tool" or not sep or not name:
        raise ConfigError(
            f"{where}: {text!r} is not a label such as 'tool:get_order'; see docs/configuration.md"
        )
    if name == "*" and not pattern:
        raise ConfigError(f"{where}: 'tool:*' is allowed only under [agent] real")
    if name != "*" and not name.isidentifier():
        raise ConfigError(f"{where}: {name!r} in {text!r} is not a tool name")
    return Label(kind, name)


def matches_real(real: list[str], label: str) -> bool:
    """Whether a real entry, such as tool:get_order or tool:*, covers this label."""
    kind = label.partition(":")[0]
    return any(entry == label or entry == f"{kind}:*" for entry in real)
