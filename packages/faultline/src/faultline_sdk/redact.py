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
            r"Illegal header (?:name|value) .*",  # h11 quotes the bad header
        ]
    )
)


# A JWT. Each run of token characters is tried once, from its first eyJ, with no backing up,
# so a large base64 body stays linear. What comes before it, such as the n of an escaped
# newline, is kept.
TOKEN = "[A-Za-z0-9_-]"
JWT = re.compile(rf"(?<!{TOKEN})((?>{TOKEN}*?eyJ)){TOKEN}{{8,}}+\.{TOKEN}{{8,}}+\.{TOKEN}{{8,}}+")

# A query or a password may hold a raw ' or \, so only these end a URL. A quote or a bracket
# that closes the URL is given back after the cut.
URL = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,30}://[^\s\"<>]+")
CLOSING = ")'\\,.;:"
LOGIN = re.compile(r"(?<=://)[^/?#\s]*@")


def plain_url(found: re.Match[str]) -> str:
    """A URL with its query, fragment and credentials marked as removed, not silently gone."""
    text = found.group()
    url = text.rstrip(CLOSING)
    kept, *cut = re.split(r"([?#])", url, maxsplit=1)
    query = f"{cut[0]}{URL_PART_REMOVED}" if cut else ""
    return f"{LOGIN.sub(f'{URL_PART_REMOVED}@', kept)}{query}{text[len(url) :]}"


def no_jwt(found: re.Match[str]) -> str:
    return found.group(1).removesuffix("eyJ") + REMOVED


def redact(text: str) -> str:
    return JWT.sub(no_jwt, PATTERNS.sub(REMOVED, URL.sub(plain_url, text)))
