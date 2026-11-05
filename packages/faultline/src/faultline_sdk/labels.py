"""Labels name a call, such as tool:<name> or model.

This version knows tool labels, model labels and, under [agent] real only, hosted MCP
servers.
"""

import re

from faultline_sdk.errors import ConfigError

# The name the model sees. Providers allow hyphens, which a Python function cannot have.
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")
PROVIDERS = ("openai",)  # providers with a built-in address table
MODEL_OPS = ("embeddings", "moderations", "input_tokens")  # calls that only read or compute
MODEL = re.compile(r"model(?::(?P<provider>[a-z*][a-z0-9_-]*)(?:/(?P<op>[a-z_]+))?)?")
HOSTED = re.compile(r"hosted:mcp/[A-Za-z0-9_.-]+")
DOCS = "see docs/configuration.md"


def kind_of(label: str) -> str:
    return label.partition(":")[0]


def check(text: str, where: str, *, pattern: bool = False) -> str:
    """Check a label and return it. pattern: an [agent] real entry, for tool:* and hosted:."""
    kind, sep, name = text.partition(":")
    if kind == "model":
        return check_model(text, where)
    if kind == "hosted" and sep:
        if not pattern:
            raise ConfigError(f"{where}: {text!r} is allowed only under [agent] real")
        if not HOSTED.fullmatch(text):
            raise ConfigError(f"{where}: {text!r} is not of the form 'hosted:mcp/<label>'")
        return text
    if kind != "tool" or not sep or not name:
        raise ConfigError(
            f"{where}: {text!r} is not a label of the form 'tool:<name>' or 'model'; {DOCS}"
        )
    if name == "*" and not pattern:
        raise ConfigError(f"{where}: 'tool:*' is allowed only under [agent] real")
    if name != "*" and not TOOL_NAME.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} in {text!r} is not a tool name")
    return text


def check_model(text: str, where: str) -> str:
    found = MODEL.fullmatch(text)
    if not found:
        raise ConfigError(f"{where}: {text!r} is not of the form 'model:<provider>'; {DOCS}")
    provider, op = found.group("provider"), found.group("op")
    if provider is not None and provider != "*" and provider not in PROVIDERS:
        raise ConfigError(f"{where}: {provider!r} in {text!r} is not a known provider; {DOCS}")
    if provider == "*" and op is None:
        raise ConfigError(f"{where}: write 'model' for every provider, not 'model:*'")
    if op is not None and op not in MODEL_OPS:
        raise ConfigError(f"{where}: {op!r} in {text!r} is not a model call kind; {DOCS}")
    return text


def matches(label: str, call: str) -> bool:
    """Whether a fault's label names a call's label exactly as written.

    A model call's label is model:<provider> for a chat and model:<provider>/<op> for a
    read path. "model" names every chat; "model:*/embeddings" every provider's embeddings.
    """
    if label == call:
        return True
    if not call.startswith("model:"):
        return False
    op = call.removeprefix("model:").partition("/")[2]
    if label == "model":
        return not op
    return bool(op) and label == f"model:*/{op}"


def matches_real(real: list[str], label: str) -> bool:
    """Whether a real entry, tool:<name> or tool:*, covers this label."""
    kind = kind_of(label)
    return any(entry == label or entry == f"{kind}:*" for entry in real)
