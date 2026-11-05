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
        ]
    )
)


def redact(text: str) -> str:
    return PATTERNS.sub(REMOVED, text)
