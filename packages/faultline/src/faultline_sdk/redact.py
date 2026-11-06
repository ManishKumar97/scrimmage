"""Remove known secret patterns from anything that is recorded."""

import re

REMOVED = "[secret removed]"
URL_PART_REMOVED = "[removed]"
PATTERNS = re.compile(
    "|".join(
        [
            r"sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{16,}",  # OpenAI and Anthropic keys
            r"AKIA[0-9A-Z]{16}",  # AWS access key ids
            r"AIza[0-9A-Za-z_-]{35}",  # Google API keys
            r"gh[pousr]_[A-Za-z0-9]{36,}",  # GitHub tokens
            r"xox[abpr]-[A-Za-z0-9-]{10,}",  # Slack tokens
            r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}",  # bearer tokens
            r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWTs
            r"Illegal header (?:name|value) .*",  # h11 quotes the bad header
        ]
    )
)


# A query or a password may hold a raw ' or \, so only these end a URL. A quote or a bracket
# that closes the URL is given back after the cut.
URL = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,30}://[^\s\"<>]+")
CLOSING = ")'\\,.;:"


def plain_url(found: re.Match[str]) -> str:
    """A URL with its query, fragment and credentials marked as removed, not silently gone."""
    text = found.group()
    url = text.rstrip(CLOSING)
    scheme, _, rest = url.partition("://")
    kept, *cut = re.split(r"([?#])", rest, maxsplit=1)
    address, slash, path = kept.partition("/")
    login, at, host = address.rpartition("@")
    credentials = f"{URL_PART_REMOVED}{at}" if login else ""
    query = f"{cut[0]}{URL_PART_REMOVED}" if cut else ""
    return f"{scheme}://{credentials}{host}{slash}{path}{query}{text[len(url) :]}"


def redact(text: str) -> str:
    return PATTERNS.sub(REMOVED, URL.sub(plain_url, text))
