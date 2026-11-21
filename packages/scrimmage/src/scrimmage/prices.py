"""Model prices as data: the file shipped in the wheel, read once.

A price changes by a plain change to prices.toml. A team's own prices under [agent.prices]
in scrimmage.toml are read by the same code, so both files take the same keys.
"""

import functools
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import cast

from scrimmage.errors import ConfigError

DOCS = "docs/configuration.md"
SHIPPED = "prices.toml"
RUNS = "scrimmage-runs"
RATE_KEYS = {"input", "output", "cached_input", "cache_write_30m", "cache_write_12h"}
PRICE_KEYS = {*RATE_KEYS, "tiers"}  # a model's keys; a tier's are RATE_KEYS
FILE_KEYS = {"as_of", "source", "models"}
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens. With no cached rate, cached tokens cost the full input
    rate: a price that guesses must guess high, or the budget could let through more. With
    no cache-write rate, a written token costs the full input rate, as on a model that
    charges nothing extra to write its cache. tiers: the rates of each service tier, by name.
    A tier not listed costs these rates."""

    input: float
    output: float
    cached_input: float | None = None
    cache_write_30m: float | None = None  # a token written to the cache for 30 minutes
    cache_write_12h: float | None = None  # for 12 hours, a Responses API choice
    tiers: tuple[tuple[str, "Rates"], ...] = ()

    def at(self, tier: str | None) -> "Rates":
        """The rates a call on this service tier pays."""
        return dict(self.tiers).get(tier or "", self)


@dataclass(frozen=True)
class PriceFile:
    as_of: str
    source: str
    models: tuple[tuple[str, Rates], ...]


def read_prices(value: object, where: str) -> tuple[tuple[str, Rates], ...]:
    """A table of model name to rates, as [agent.prices] and a price file's [models] write it."""
    return tuple(
        (model, read_rates(as_table(raw, f"{where} {model}"), f"{where} {model}"))
        for model, raw in as_table(value, where).items()
    )


def read_rates(entry: dict[str, object], item: str, *, tiers: bool = True) -> Rates:
    known = PRICE_KEYS if tiers else RATE_KEYS
    for key in entry:
        if key not in known:
            raise ConfigError(f"{item} {key}: unknown setting; see {DOCS}")
    rate, output = dollars(entry, "input", item), dollars(entry, "output", item)
    cached = dollars(entry, "cached_input", item) if "cached_input" in entry else None
    if cached is not None and cached > rate:
        raise ConfigError(
            f"{item} cached_input: {cached:g} is above input, {rate:g}; a cached token "
            "costs at most a full one"
        )
    writes: list[float | None] = []
    for key in ("cache_write_30m", "cache_write_12h"):
        write = dollars(entry, key, item) if key in entry else None
        if write is not None and write < rate:
            raise ConfigError(
                f"{item} {key}: {write:g} is below input, {rate:g}; a token written to "
                "the cache costs at least a full one"
            )
        writes.append(write)
    named: list[tuple[str, Rates]] = []
    for tier, raw in as_table(entry.get("tiers", {}), f"{item} tiers").items():
        where = f"{item} tiers {tier}"
        if not tier.strip():
            raise ConfigError(f"{item} tiers: a tier needs a name")
        named.append((tier, read_rates(as_table(raw, where), where, tiers=False)))
    return Rates(rate, output, cached, *writes, tiers=tuple(named))


def dollars(entry: dict[str, object], key: str, item: str) -> float:
    number = entry.get(key)
    if (
        isinstance(number, bool)
        or not isinstance(number, int | float)
        or not math.isfinite(number)
        or number < 0
    ):
        raise ConfigError(f"{item} {key}: expected dollars per million tokens")
    return float(number)


def as_table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def parse(text: str, name: str) -> PriceFile:
    """Read a price file. A bad file is a ConfigError that names the file and the line."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{name}: {exc}") from None
    for key in data:
        if key not in FILE_KEYS:
            raise ConfigError(f"{name} {key}: unknown setting; see {DOCS}")
    as_of, source = data.get("as_of"), data.get("source")
    try:
        if not isinstance(as_of, str):
            raise ValueError
        date.fromisoformat(as_of)
    except ValueError:
        raise ConfigError(f'{name} as_of: expected a date such as "2026-11-19"') from None
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(f"{name} source: expected text, the page the prices came from")
    table = as_table(data.get("models", {}), f"{name} models")
    if not table:
        raise ConfigError(f"{name}: [models] needs at least one model")
    models: list[tuple[str, Rates]] = []
    for model, raw in table.items():
        try:
            item = f"{name} models {model}"
            rates = read_rates(as_table(raw, item), item)
            check_file_rates(model, rates, item)
            models.append((model, rates))
        except ConfigError as exc:
            raise ConfigError(f"{exc.problems[0]}{line_of(text, model)}") from None
    return PriceFile(as_of, source, tuple(models))


RESERVED_TIERS = ("default", "auto")


def check_file_rates(model: str, rates: Rates, item: str) -> None:
    """What a price file may not hold, as it could turn the budget off for a model.

    [agent.prices] may price a model at 0, as the team's own choice. A file is fetched, and
    kept where no commit shows it, so it may not: input must be above 0, and so must output
    unless the model is an embedding model, which has no output. A tier cannot be named
    default or auto, which are not tiers OpenAI bills at a rate of their own.
    """
    if rates.input <= 0:
        raise ConfigError(
            f"{item} input: {rates.input:g} is not above 0; a price file may not make a model free"
        )
    if rates.output <= 0 and "embedding" not in model:
        raise ConfigError(
            f"{item} output: {rates.output:g} is not above 0; "
            "only an embedding model has no output price"
        )
    for tier, tiered in rates.tiers:
        if tier in RESERVED_TIERS:
            raise ConfigError(f"{item} tiers {tier}: {tier} is not a tier a file may price")
        check_file_rates(model, tiered, f"{item} tiers {tier}")


def line_of(text: str, model: str) -> str:
    """' (line N)' for the table that names this model, or '' if it cannot be found."""
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("[") and f'"{model}"' in line:
            return f" (line {number})"
    return ""


@functools.cache
def shipped() -> PriceFile:
    text = resources.files("scrimmage").joinpath(SHIPPED).read_text(encoding="utf-8")
    return parse(text, SHIPPED)


def price(model: str, *tables: dict[str, Rates]) -> Rates | None:
    """The first table that names the model, whole. Tables are in order of authority.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. Nothing
    else does: gpt-4o-audio-preview is not priced like gpt-4o. An exact name wins over its
    family in every table.
    """
    for name in (model, DATED.sub("", model)):
        for table in tables:
            if name in table:
                return table[name]
    return None


def team_path(root: Path) -> Path:
    return root / RUNS / SHIPPED


def team(root: Path) -> PriceFile | None:
    """scrimmage-runs/prices.toml beside scrimmage.toml, if `scrimmage prices update` wrote it."""
    try:
        text = team_path(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{RUNS}/{SHIPPED}: cannot read it: {exc}") from None
    return parse(text, f"{RUNS}/{SHIPPED}")


def in_order(
    agent: tuple[tuple[str, Rates], ...], file: PriceFile | None
) -> tuple[tuple[str, Rates], ...]:
    """The rows a run prices with before the shipped file: [agent.prices], then the team's
    file. A model in both takes the first."""
    named = {model for model, _ in agent}
    rows = () if file is None else tuple(row for row in file.models if row[0] not in named)
    return agent + rows


def age(file: PriceFile | None) -> str:
    """The as_of date of the file that prices a model the team did not price itself."""
    return (file or shipped()).as_of


def sources(agent: tuple[tuple[str, Rates], ...], file: PriceFile | None) -> list[str]:
    """The lines `scrimmage check` adds when the team has its own prices: each source, and how
    old a file's are. With none, the one budget line says it all."""
    if not agent and file is None:
        return []
    lines = [f"price {model}: [agent.prices]" for model, _ in agent]
    if file is not None:
        lines.append(f"prices: {RUNS}/{SHIPPED}, as of {file.as_of} ({len(file.models)} models)")
    lines.append(
        f"prices: the shipped {SHIPPED}, as of {shipped().as_of} ({len(shipped().models)} models)"
    )
    return lines
