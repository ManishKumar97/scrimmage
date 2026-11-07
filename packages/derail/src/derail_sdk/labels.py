"""Labels name a call, such as tool:<name> or model.

This version knows tool labels, model labels, web labels, MCP labels and, under
[agent] real only, hosted MCP servers and process:python.
"""

import ipaddress
import re

from derail_sdk.errors import ConfigError

# The name the model sees. Providers allow hyphens, which a Python function cannot have.
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")
PROVIDERS = ("openai",)  # providers with a built-in address table
MODEL_OPS = ("embeddings", "moderations", "input_tokens")  # calls that only read or compute
MODEL = re.compile(r"model(?::(?P<provider>[a-z*][a-z0-9_-]*)(?:/(?P<op>[a-z_]+))?)?")
HOSTED = re.compile(r"hosted:mcp/[A-Za-z0-9_.-]+")
WEB_HOST = re.compile(r"[a-z0-9][a-z0-9._-]*")
WEB_METHOD = re.compile(r"[A-Za-z][A-Za-z-]*")  # letters, and the hyphen of M-SEARCH
WEB_PATH = re.compile(r"/(?:(?![?#])[\x21-\x7e])*")  # as sent: percent-encoded, no query
PROCESS = "process:python"  # a Python child that would run with no gates
DOCS = "see docs/configuration.md"
NAME_CAP = 200  # a model name comes from a request, so a line shows this much of it


def kind_of(label: str) -> str:
    return label.partition(":")[0]


def tool_name(path: str, renames: dict[str, str]) -> str:
    """The name the model sees for a tool listed by import path."""
    return renames.get(path, path.rpartition(":")[2])


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
    if text == PROCESS:
        if not pattern:
            raise ConfigError(f"{where}: {text!r} is allowed only under [agent] real")
        return text
    if kind == "http" and sep:
        return check_web(text, where, pattern=pattern)
    if kind == "mcp" and sep:
        return check_mcp(text, where)
    if kind != "tool" or not sep or not name:
        raise ConfigError(
            f"{where}: {text!r} is not a label of the form 'tool:<name>', 'mcp:<tool>', "
            f"'http:<host>' or 'model'; {DOCS}"
        )
    if name == "*" and not pattern:
        raise ConfigError(f"{where}: 'tool:*' is allowed only under [agent] real")
    if name != "*" and not TOOL_NAME.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} in {text!r} is not a tool name")
    return text


def check_mcp(text: str, where: str) -> str:
    """mcp:<tool> or mcp:<server>/<tool>. A server chooses its own name, so any text will do."""
    name = text.removeprefix("mcp:")
    if not name or "*" in name or not name.isprintable() or name != name.strip(" /"):
        raise ConfigError(
            f"{where}: {text!r} is not of the form 'mcp:<tool>' or 'mcp:<server>/<tool>'; {DOCS}"
        )
    return text


def check_web(text: str, where: str, *, pattern: bool) -> str:
    """http:<host>, and under [agent] real also http:<host> METHOD /path. Hosts are lowercase."""
    parts = text.removeprefix("http:").split(" ")
    host = web_host(parts[0], text, where)
    if len(parts) == 1:
        return f"http:{host}"
    form = "'http:<host> METHOD /path'"
    if not pattern:
        raise ConfigError(f"{where}: name the method and path in their own settings, not here")
    if len(parts) > 3 or not WEB_METHOD.fullmatch(parts[1]):
        raise ConfigError(f"{where}: {text!r} is not of the form {form}")
    if len(parts) == 2:
        return f"http:{host} {parts[1].upper()}"
    if not WEB_PATH.fullmatch(parts[2]):
        raise ConfigError(f"{where}: {text!r} is not of the form {form}, with no query string")
    return f"http:{host} {parts[1].upper()} {parts[2]}"


def method_of(value: str, where: str) -> str:
    if not WEB_METHOD.fullmatch(value):
        raise ConfigError(f"{where} method: {value!r} is not an HTTP method")
    return value.upper()


def web_host(text: str, label: str, where: str) -> str:
    """A host as a request's URL has it: lowercase, no port, an IPv6 address unbracketed."""
    host = text.lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ConfigError(
                f"{where}: {label!r} has a port; a label names the host only"
            ) from None
        return host
    if not WEB_HOST.fullmatch(host):
        raise ConfigError(f"{where}: {label!r} needs a host, of the form 'http:<host>'; {DOCS}")
    return host


def path_of(value: str, where: str) -> str:
    if not WEB_PATH.fullmatch(value):
        raise ConfigError(
            f"{where} path: {value!r} is not a path as a URL writes it, percent-encoded and "
            "with no query string"
        )
    return value


def shown_name(name: str) -> str:
    """A model name as a line can show it: escaped, so it cannot forge a line, and cut from the
    middle, as two names often differ only at the end, such as a version."""
    text = repr(name)
    if len(text) <= NAME_CAP:
        return text
    kept = NAME_CAP - 3
    return f"{text[: kept // 2]}...{text[-(kept - kept // 2) :]}"


def model_name(value: object, where: str) -> str:
    """A model's name as a request's body has it. It is compared whole, so a name that could
    never be in a request is a mistake here, not a row of model calls that fires nothing."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "*" in value
        or not value.isprintable()
    ):
        raise ConfigError(
            f"{where} model: {value!r} is not a model name; write the whole name your code "
            "sends, as text, with no wildcard; see docs/model-faults.md"
        )
    return value


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


def matches_real(
    real: list[str], label: str, method: str | None = None, path: str | None = None
) -> bool:
    """Whether a real entry covers this call: tool:<name>, tool:*, or a web label.

    A web label covers its host, or a method on it, or one method and path.
    """
    named = [label, f"{kind_of(label)}:*"]
    if method is not None:
        named += [f"{label} {method}", f"{label} {method} {path}"]
    return any(entry in named for entry in real)
