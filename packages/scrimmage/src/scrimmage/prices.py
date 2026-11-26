"""Model prices as data: the file shipped in the wheel, read once.

A price changes by a plain change to prices.toml. The file has one section for each provider,
with its own url and as_of, and the provider's models under it: [openai], [openai.models."x"].
A team's own prices under [agent.prices] in scrimmage.toml are flat, by model name, and are read
by the same code, so both take the same rate keys.
"""

import dataclasses
import functools
import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import cast

from scrimmage.errors import ConfigError, docs_url
from scrimmage.outcomes import plural

DOCS = docs_url("configuration")
SHIPPED = "prices.toml"
RUNS = "scrimmage-runs"
RATE_KEYS_ORDER = (
    "input",
    "output",
    "cached_input",
    "cache_write_30m",
    "cache_write_12h",
    "cache_write_5m",
    "cache_write_1h",
)
WRITE_KEYS = tuple(key for key in RATE_KEYS_ORDER if key.startswith("cache_write_"))
RATE_KEYS = set(RATE_KEYS_ORDER)
# What a provider charges for a request of a kind its reply counts, in dollars for each request,
# not for a million tokens: a web search. A model's row may set one, which beats the provider's own.
FEE_KEYS = ("web_search_request",)
PRICE_KEYS = {*RATE_KEYS, *FEE_KEYS, "tiers", "long"}  # a model's keys
TIER_KEYS = {*RATE_KEYS, "long"}  # a tier's keys
LONG_KEYS = {*RATE_KEYS, "above"}  # a long table's keys: the rates, and where they start
SECTION_KEYS = {"url", "as_of", "models", "fees"}
OLD_KEYS = {"as_of", "source", "models"}  # the top-level keys of the one-provider file
SECTION_NAME = re.compile(r"[a-z][a-z0-9_-]*$")
# A dated snapshot name: OpenAI's gpt-4o-mini-2024-07-18, and Anthropic's claude-haiku-4-5-20251001.
# The eight digits must read as a real date, so a model whose name ends in some other 8-digit
# number is not taken for a snapshot of its shorter name.
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")
COMPACT = re.compile(r"-(\d{8})$")


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
    cache_write_5m: float | None = None  # a token written to the cache for 5 minutes
    cache_write_1h: float | None = None  # for 1 hour
    # What the provider attaches to a row it hands out, which a price file never holds, and which
    # leave a row equal to the row it was made from.
    # aliases: pairs of tier names that are one tier for this provider, such as OpenAI's fast and
    # priority. Each provider owns its own: another provider's priority tier is no fast mode.
    aliases: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    # fees: dollars for each request of a kind a reply counts, such as a web search.
    fees: tuple[tuple[str, float], ...] = field(default=(), compare=False)
    # models: the provider's other rows, for a call that more than one model served; name: the
    # model this row prices.
    models: tuple[tuple[str, "Rates"], ...] = field(default=(), compare=False)
    name: str = field(default="", compare=False)

    def at(self, tier: str | None, aliases: tuple[tuple[str, str], ...] | None = None) -> "Rates":
        """The rates a call on this service tier pays. A provider whose tier has two names, such
        as OpenAI's fast and priority, gives them as aliases, and a row that writes only one of
        them prices both. aliases default to the row's own, which the provider attached."""
        named = dict(self.tiers)
        name = tier or ""
        same = dict(self.aliases if aliases is None else aliases)
        found = named.get(name) or named.get(same.get(name, ""))
        return self if found is None else found

    def unpriced(self, tier: str | None) -> bool:
        """True when a reply names a tier that is neither the default tier nor one this row
        prices, so its rates are not known."""
        if not tier or tier.lower() in RESERVED_TIERS:
            return False
        return self.at(tier) is self

    def sized(self, tokens_in: int) -> "Rates":
        """The rates a call with this many input tokens pays: the long rates when it is over."""
        if self.long is not None and self.above is not None and tokens_in > self.above:
            return self.long
        return self


@dataclass(frozen=True)
class Section:
    """One provider's prices: the page they came from, the day they were checked, the models."""

    url: str
    as_of: str
    models: tuple[tuple[str, Rates], ...]
    fees: tuple[tuple[str, float], ...] = ()  # [provider.fees]: dollars for each request


@dataclass(frozen=True)
class PriceFile:
    """The sections of a price file, by provider name, in file order."""

    sections: tuple[tuple[str, Section], ...]

    def section(self, provider: str) -> Section | None:
        return dict(self.sections).get(provider)

    def models(self, provider: str) -> tuple[tuple[str, Rates], ...]:
        """The provider's rows, or none if the file has no section for it."""
        found = self.section(provider)
        return () if found is None else found.models


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
    writes: dict[str, float | None] = {}
    for key in WRITE_KEYS:
        write = dollars(entry, key, item) if key in entry else None
        if write is not None and write < rate:
            raise ConfigError(
                f"{item} {key}: {write:g} is below input, {rate:g}; a token written to "
                "the cache costs at least a full one"
            )
        writes[key] = write
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
    fees = tuple((key, fee_dollars(entry, key, item)) for key in FEE_KEYS if key in entry)
    rates = Rates(
        rate,
        output,
        cached,
        writes["cache_write_30m"],
        writes["cache_write_12h"],
        tiers=tuple(named),
        above=above,
        cache_write_5m=writes["cache_write_5m"],
        cache_write_1h=writes["cache_write_1h"],
        fees=fees,
    )
    if level == "long" or "long" not in entry:
        return rates
    where = f"{item} long"
    longer = read_rates(as_table(entry["long"], where), where, level="long")
    check_long(rates, longer, where)
    return dataclasses.replace(
        rates, above=longer.above, long=dataclasses.replace(longer, above=None)
    )


def read_fees(value: object, where: str) -> tuple[tuple[str, float], ...]:
    """A provider's [fees] table: dollars for each request, read like a rate."""
    found = as_table(value, where)
    for key in found:
        if key not in FEE_KEYS:
            raise ConfigError(f"{where} {key}: unknown fee; the fees are {', '.join(FEE_KEYS)}")
    return tuple((key, fee_dollars(found, key, where)) for key in FEE_KEYS if key in found)


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


def fee_dollars(entry: dict[str, object], key: str, item: str) -> float:
    """A fee: dollars for each request, not for a million tokens."""
    number = entry.get(key)
    if (
        isinstance(number, bool)
        or not isinstance(number, int | float)
        or not math.isfinite(number)
        or number < 0
    ):
        raise ConfigError(f"{item} {key}: expected dollars for each request, such as 0.01")
    return float(number)


def as_table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def dashed_family(model: str) -> str:
    """The name of the model a snapshot dated -YYYY-MM-DD belongs to, or the name itself: how
    OpenAI dates a snapshot. A provider's `snapshot_family` is this or compact_family."""
    return DATED.sub("", model)


def compact_family(model: str) -> str:
    """The same for a snapshot dated -YYYYMMDD. Eight digits that are not a date, such as
    -12345678, are part of the name."""
    found = COMPACT.search(model)
    if found is not None:
        digits = found.group(1)
        try:
            date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError:
            return model
        return model[: found.start()]
    return model


def parse(text: str, name: str) -> PriceFile:
    """Read a price file. A bad file is a ConfigError that names the file and the line."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{name}: {exc}") from None
    except RecursionError:
        raise ConfigError(f"{name}: tables or arrays nested too deeply to read") from None
    if OLD_KEYS & data.keys():
        raise ConfigError(f"{name}: this file is in the old format; run `scrimmage prices update`")
    sections: list[tuple[str, Section]] = []
    for provider in data:
        if not SECTION_NAME.match(provider) or not isinstance(data[provider], dict):
            raise ConfigError(
                f"{name} {provider}: expected a provider's section, such as [openai]; see {DOCS}"
            )
        sections.append(
            (provider, parse_section(text, f"{name} {provider}", provider, data[provider]))
        )
    if not sections:
        raise ConfigError(f"{name}: needs at least one provider's section, such as [openai]")
    return PriceFile(tuple(sections))


def parse_section(text: str, item: str, provider: str, raw: object) -> Section:
    table = as_table(raw, item)
    for key in table:
        if key not in SECTION_KEYS:
            raise ConfigError(f"{item} {key}: unknown setting; see {DOCS}")
    as_of, url = table.get("as_of"), table.get("url")
    try:
        if not isinstance(as_of, str):
            raise ValueError
        date.fromisoformat(as_of)
    except ValueError:
        raise ConfigError(f'{item} as_of: expected a date such as "2026-11-19"') from None
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{item} url: expected text, the page the prices came from")
    rows = as_table(table.get("models", {}), f"{item} models")
    if not rows:
        raise ConfigError(f"{item}: [{provider}.models] needs at least one model")
    models: list[tuple[str, Rates]] = []
    for model, entry in rows.items():
        try:
            where = f"{item} models {model}"
            rates = read_rates(as_table(entry, where), where)
            check_file_rates(model, rates, where)
            models.append((model, rates))
        except ConfigError as exc:
            raise ConfigError(f"{exc.problems[0]}{line_of(text, provider, model)}") from None
    fees = read_fees(table.get("fees", {}), f"{item} fees")
    for key, fee in fees:
        if fee < FEE_FLOOR:
            raise ConfigError(
                f"{item} fees {key}: {fee:g} is below {FEE_FLOOR:g}, the least a price file may "
                "state for a request; a price file may not make it free"
            )
    return Section(url, as_of, tuple(models), fees)


RESERVED_TIERS = ("default", "auto")
# Dollars per million tokens. The cheapest real price today is 0.01, so no honest file is
# below this, and a file that is has made the model free in all but name.
FLOOR = 0.001
# The same for a fee, in dollars for each request, whose honest values are cents: a file that
# states less has made the request free in all but name.
FEE_FLOOR = 0.0001


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
    for key in ("cached_input", *WRITE_KEYS):
        value = getattr(rates, key)
        if value is not None and value < FLOOR:
            raise ConfigError(
                f"{item} {key}: {value:g} is below {FLOOR:g}, the least a price file may state"
            )
    for key, fee in rates.fees:
        if fee < FEE_FLOOR:
            raise ConfigError(
                f"{item} {key}: {fee:g} is below {FEE_FLOOR:g}, the least a price file may "
                "state for a request; a price file may not make it free"
            )
    for tier, tiered in rates.tiers:
        if tier.lower() in RESERVED_TIERS:
            raise ConfigError(f"{item} tiers {tier}: {tier} is not a tier a file may price")
        check_file_rates(model, tiered, f"{item} tiers {tier}")


def line_of(text: str, provider: str, model: str) -> str:
    """' (line N)' for the table that names this model in this provider's section, or '' if it
    cannot be found."""
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(f"[{provider}.models.") and f'"{model}"' in line:
            return f" (line {number})"
    return ""


@functools.cache
def shipped() -> PriceFile:
    text = resources.files("scrimmage").joinpath(SHIPPED).read_text(encoding="utf-8")
    return parse(text, SHIPPED)


def price(model: str, *tables: dict[str, Rates], family: Callable[[str], str]) -> Rates | None:
    """The first table that names the model, whole. Tables are in order of authority, and a
    table's say comes before any table below it: a family row in [agent.prices] beats an
    exact dated row in a file. Within one table, the exact name beats its family.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. What a snapshot
    is, is the provider's rule, `family`. Nothing else does: gpt-4o-audio-preview is not priced
    like gpt-4o.
    """
    for table in tables:
        for name in (model, family(model)):
            if name in table:
                return table[name]
    return None


def priced(
    provider: str, model: str, *tables: dict[str, Rates], family: Callable[[str], str]
) -> Rates | None:
    """price(), with the shipped section for this provider as the last table. A model is looked
    up in its own provider's section only: another provider's row of the same name is not it."""
    return price(model, *tables, dict(shipped().models(provider)), family=family)


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
    agent: tuple[tuple[str, Rates], ...],
    file: tuple[tuple[str, Rates], ...],
    family: Callable[[str], str],
) -> tuple[tuple[str, Rates], ...]:
    """The rows a run prices a provider's calls with before the shipped file: [agent.prices],
    then the team's file section for that provider. The two are one table once merged, so a
    file row is left out when the team's own rows already speak for its name: the same name,
    or the family of a dated name."""
    named = {model for model, _ in agent}
    rows = tuple(row for row in file if row[0] not in named and family(row[0]) not in named)
    return agent + rows


def shown(provider: str) -> str:
    """The name a provider goes by in what `scrimmage check` prints: the provider's own
    display_name, or the section's raw name when no registered provider has it."""
    from scrimmage.providers.registry import built_in

    try:
        return built_in().get(provider).display_name
    except ValueError:
        return provider


def unregistered(file: PriceFile | None) -> list[str]:
    """The sections of this file that name no registered provider: a typo, or a provider this
    version of Scrimmage does not gate. Such a section is read and never used."""
    from scrimmage.providers.registry import built_in

    known = {p.name for p in built_in().providers}
    return [] if file is None else [name for name, _ in file.sections if name not in known]


def unused_fees(file: PriceFile | None) -> list[str]:
    """Each fee a section of this file sets that its provider does not charge, as
    'openai web_search_request'. Such a fee is read and never used."""
    from scrimmage.providers.registry import built_in

    if file is None:
        return []
    named = {p.name: p.fees for p in built_in().providers}
    return [
        f"{provider} {key}"
        for provider, section in file.sections
        if provider in named
        for key, _ in section.fees
        if key not in named[provider]
    ]


def age(file: PriceFile | None) -> str:
    """Each provider's price date, as `scrimmage check` shows it: 'OpenAI prices as of
    2026-11-22'. A section of the team's file stands for the shipped one, as it is read first."""
    dates = {provider: section.as_of for provider, section in shipped().sections}
    if file is not None:
        dates.update({provider: section.as_of for provider, section in file.sections})
    return ", ".join(f"{shown(p)} prices as of {as_of}" for p, as_of in dates.items())


def sources(agent: tuple[tuple[str, Rates], ...], file: PriceFile | None) -> list[str]:
    """The lines `scrimmage check` adds when the team has its own prices: each source, and how
    old each provider's prices in a file are. With none, the one budget line says it all."""
    if not agent and file is None:
        return []
    lines = [f"price {model}: [agent.prices]" for model, _ in agent]
    for where, found in ((f"{RUNS}/{SHIPPED}", file), (f"the shipped {SHIPPED}", shipped())):
        for provider, section in () if found is None else found.sections:
            lines.append(
                f"prices: {where}, {shown(provider)} prices as of "
                f"{section.as_of} ({plural(len(section.models), 'model')})"
            )
    return lines


def source_list(
    agent: tuple[tuple[str, Rates], ...], file: PriceFile | None
) -> list[dict[str, object]]:
    """The places prices come from, in order of authority, for `scrimmage check --json`. A file
    is listed once for each provider section it holds."""
    found: list[dict[str, object]] = []
    if agent:
        found.append(
            {"source": "[agent.prices]", "provider": None, "as_of": None, "models": len(agent)}
        )
    if file is not None:
        found += file_sources(f"{RUNS}/{SHIPPED}", file)
    found += file_sources(f"the shipped {SHIPPED}", shipped())
    return found


def file_sources(where: str, file: PriceFile) -> list[dict[str, object]]:
    return [
        {
            "source": where,
            "provider": provider,
            "as_of": section.as_of,
            "models": len(section.models),
        }
        for provider, section in file.sections
    ]
