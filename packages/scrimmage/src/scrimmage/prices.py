"""Model prices as data: the file shipped in the wheel, read once.

A price changes by a plain change to prices.toml. A team's own prices under [agent.prices]
in scrimmage.toml are read by the same code, so both files take the same keys.
"""

import dataclasses
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
RATE_KEYS_ORDER = ("input", "output", "cached_input", "cache_write_30m", "cache_write_12h")
RATE_KEYS = set(RATE_KEYS_ORDER)
PRICE_KEYS = {*RATE_KEYS, "tiers", "long"}  # a model's keys
TIER_KEYS = {*RATE_KEYS, "long"}  # a tier's keys
LONG_KEYS = {*RATE_KEYS, "above"}  # a long table's keys: the rates, and where they start
FILE_KEYS = {"as_of", "source", "models"}
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens. With no cached rate, cached tokens cost the full input
    rate: a price that guesses must guess high, or the budget could let through more. With
    no cache-write rate, a written token costs the full input rate, as on a model that
    charges nothing extra to write its cache. tiers: the rates of each service tier, by name.
    A tier not listed costs these rates. long: the rates of a call whose input is over `above`
    tokens, for a model that charges more for a long prompt. OpenAI charges the whole call at
    them, input and output, not only the tokens past the threshold."""

    input: float
    output: float
    cached_input: float | None = None
    cache_write_30m: float | None = None  # a token written to the cache for 30 minutes
    cache_write_12h: float | None = None  # for 12 hours, a Responses API choice
    tiers: tuple[tuple[str, "Rates"], ...] = ()
    above: int | None = None  # input tokens; a call with more pays the long rates
    long: "Rates | None" = None

    def at(self, tier: str | None) -> "Rates":
        """The rates a call on this service tier pays."""
        return dict(self.tiers).get(tier or "", self)

    def sized(self, tokens_in: int) -> "Rates":
        """The rates a call with this many input tokens pays: the long rates when it is over."""
        if self.long is not None and self.above is not None and tokens_in > self.above:
            return self.long
        return self


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


def read_rates(entry: dict[str, object], item: str, *, level: str = "model") -> Rates:
    """level is model, tier or long, and says which keys the table may hold."""
    known = {"model": PRICE_KEYS, "tier": TIER_KEYS, "long": LONG_KEYS}[level]
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
    if level == "model":
        for tier, raw in as_table(entry.get("tiers", {}), f"{item} tiers").items():
            where = f"{item} tiers {tier}"
            if not tier.strip():
                raise ConfigError(f"{item} tiers: a tier needs a name")
            named.append((tier, read_rates(as_table(raw, where), where, level="tier")))
    above: int | None = None
    if level == "long":
        found = entry.get("above")
        if isinstance(found, bool) or not isinstance(found, int) or found < 1:
            raise ConfigError(
                f"{item} above: expected a whole number of input tokens, at least 1, such as 272000"
            )
        above = found
    rates = Rates(rate, output, cached, *writes, tiers=tuple(named), above=above)
    if level == "long" or "long" not in entry:
        return rates
    where = f"{item} long"
    longer = read_rates(as_table(entry["long"], where), where, level="long")
    check_long(rates, longer, where)
    return dataclasses.replace(
        rates, above=longer.above, long=dataclasses.replace(longer, above=None)
    )


def check_long(base: Rates, longer: Rates, item: str) -> None:
    """A long call may not cost less per token than a short one, in any kind of token. A rate
    a table leaves out costs its input rate, so that is what is compared."""
    for key in RATE_KEYS_ORDER:
        was, now = effective(base, key), effective(longer, key)
        if now < was:
            raise ConfigError(
                f"{item} {key}: {now:g} is below the {was:g} of a short call; "
                "a long call costs at least as much per token"
            )


def effective(rates: Rates, key: str) -> float:
    """What a token of this kind costs: a rate left out costs the input rate."""
    value: float | None = getattr(rates, key)
    return rates.input if value is None else value


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
    except RecursionError:
        raise ConfigError(f"{name}: tables or arrays nested too deeply to read") from None
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
# Dollars per million tokens. The cheapest real price today is 0.02, so no honest file is
# below this, and a file that is has made the model free in all but name.
FLOOR = 0.001


def check_file_rates(model: str, rates: Rates, item: str) -> None:
    """What a price file may not hold, as it could turn the budget off for a model.

    [agent.prices] may price a model at 0, as the team's own choice. A file is fetched, and
    kept where no commit shows it, so it may not: input must be at least FLOOR, and so must
    output unless the model's name contains embedding, as such a model has no output. A cached
    or cache-write rate, if given, must reach FLOOR too, with no exception. A tier
    cannot be named default or auto, in any case, which are not tiers OpenAI bills at a rate
    of their own. A long table needs no floor of its own: each of its rates is at least the
    same rate of its base, which has passed.
    """
    if rates.input < FLOOR:
        raise ConfigError(
            f"{item} input: {rates.input:g} is below {FLOOR:g}, the least a price file may "
            "state; a price file may not make a model free"
        )
    if rates.output < FLOOR and "embedding" not in model:
        raise ConfigError(
            f"{item} output: {rates.output:g} is below {FLOOR:g}, the least a price file may "
            "state, except for a model whose name contains embedding"
        )
    for key in ("cached_input", "cache_write_30m", "cache_write_12h"):
        value = getattr(rates, key)
        if value is not None and value < FLOOR:
            raise ConfigError(
                f"{item} {key}: {value:g} is below {FLOOR:g}, the least a price file may state"
            )
    for tier, tiered in rates.tiers:
        if tier.lower() in RESERVED_TIERS:
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
    """The first table that names the model, whole. Tables are in order of authority, and a
    table's say comes before any table below it: a family row in [agent.prices] beats an
    exact dated row in a file. Within one table, the exact name beats its family.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. Nothing
    else does: gpt-4o-audio-preview is not priced like gpt-4o.
    """
    for table in tables:
        for name in (model, DATED.sub("", model)):
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
    file. The two are one table once merged, so a file row is left out when the team's own
    rows already speak for its name: the same name, or the family of a dated name."""
    named = {model for model, _ in agent}
    rows = ()
    if file is not None:
        rows = tuple(
            row for row in file.models if row[0] not in named and DATED.sub("", row[0]) not in named
        )
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


def source_list(
    agent: tuple[tuple[str, Rates], ...], file: PriceFile | None
) -> list[dict[str, object]]:
    """The places prices come from, in order of authority, for `scrimmage check --json`."""
    found: list[dict[str, object]] = []
    if agent:
        found.append({"source": "[agent.prices]", "as_of": None, "models": len(agent)})
    if file is not None:
        found.append(
            {"source": f"{RUNS}/{SHIPPED}", "as_of": file.as_of, "models": len(file.models)}
        )
    found.append(
        {
            "source": f"the shipped {SHIPPED}",
            "as_of": shipped().as_of,
            "models": len(shipped().models),
        }
    )
    return found
