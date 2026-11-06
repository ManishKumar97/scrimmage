"""Remove known secret patterns from anything that is recorded."""

import re

REMOVED = "[secret removed]"
PATTERNS = re.compile(
    "|".join(
        [
            r"sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{16,}",  # OpenAI and Anthropic keys
            r"AKIA[0-9A-Z]{16}",  # AWS access key ids
            r"AIza[0-9A-Za-z_-]{35}",  # Google API keys
            r"gh[pousr]_[A-Za-z0-9]{36,}",  # GitHub tokens
            r"xox[abpr]-[A-Za-z0-9-]{10,}",  # Slack tokens
            r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}",  # bearer tokens
            r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWTs
            r"Illegal header (?:name|value) .*",  # h11 quotes the bad header
        ]
    )
)


URL = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,30}://[^\s'\"<>\\]+")


def plain_url(found: re.Match[str]) -> str:
    """A URL without its query, fragment or credentials, which an error message may quote."""
    scheme, _, rest = found.group().partition("://")
    address, slash, path = re.split(r"[?#]", rest, maxsplit=1)[0].partition("/")
    return f"{scheme}://{address.rpartition('@')[2]}{slash}{path}"


def redact(text: str) -> str:
    return URL.sub(plain_url, PATTERNS.sub(REMOVED, text))
