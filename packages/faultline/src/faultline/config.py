"""The user's settings, read from a TOML file.

    [attributes]   # span attributes that hold the run's question, answer and stop reason
    question = "my.question"

    [detect]
    tool_timeout_seconds = 30.0

    [checks]
    repeated_call_limit = 3

    [cost]         # US dollars per million tokens, for faultline test's estimate and cap
    input_per_mtok = 3.0
    output_per_mtok = 15.0

A key this module does not know is an error that names it, so a typo is never silent.
"""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = Path("faultline.toml")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Attributes:
    """Names of the span attributes that hold facts about a run. None means not mapped."""

    question: str | None = None
    answer: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DetectConfig:
    tool_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ChecksConfig:
    repeated_call_limit: int = 3


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Model prices. None means not given: there is no built-in price table."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


@dataclass(frozen=True, slots=True)
class Config:
    attributes: Attributes = field(default_factory=Attributes)
    detect: DetectConfig = field(default_factory=DetectConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    cost: CostConfig = field(default_factory=CostConfig)


# (section, key): (limit, the value must be strictly above it)
MINIMUMS = {
    ("detect", "tool_timeout_seconds"): (0, True),
    ("checks", "repeated_call_limit"): (1, False),
    ("cost", "input_per_mtok"): (0, False),
    ("cost", "output_per_mtok"): (0, False),
}
SECTIONS = {
    "attributes": Attributes,
    "detect": DetectConfig,
    "checks": ChecksConfig,
    "cost": CostConfig,
}


def section_of(cls, name: str, table: object):
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    kinds = {f.name: f.type for f in fields(cls)}
    known = list(kinds)
    values = {}
    for key, value in table.items():
        if key not in known:
            raise ConfigError(f"unknown key {name}.{key}; known: {', '.join(known)}")
        kind = kinds[key]
        if kind in (float, float | None):  # TOML writes 30 for 30.0
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ConfigError(f"{name}.{key} has the wrong type: {value!r}")
        if (name, key) in MINIMUMS:
            least, strict = MINIMUMS[name, key]
            if value < least or (strict and value == least):
                relation = ">" if strict else ">="
                raise ConfigError(f"{name}.{key} must be {relation} {least}: {value!r}")
        values[key] = value
    return cls(**values)


def parse_config(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"not valid TOML: {error}") from error
    for name in data:
        if name not in SECTIONS:
            raise ConfigError(f"unknown section [{name}]; known: {', '.join(SECTIONS)}")
    sections = {n: section_of(SECTIONS[n], n, data[n]) for n in data}
    return Config(**sections)


def load_config(path: Path | None = None) -> Config:
    """The config at `path`, else ./faultline.toml if it exists, else the defaults."""
    if path is None:
        if not DEFAULT_PATH.is_file():
            return Config()
        path = DEFAULT_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error
    try:
        return parse_config(text)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from error
