"""The fault catalog: every fault Faultline injects names where it was seen for real."""

import re
import tomllib
from collections import Counter
from importlib.resources import files

from faultline.faults import known

OBSERVED = re.compile(r"^observed: [\w.-]+\.jsonl, trace [0-9a-f]{32}$")
INCIDENT = re.compile(r"^incident: https://\S+$")


def catalog() -> list[dict]:
    """The catalog as installed with the package."""
    text = files("faultline").joinpath("catalog.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["fault"]


def test_every_registered_fault_kind_has_exactly_one_entry():
    assert Counter(entry["spec"] for entry in catalog()) == Counter(known())


def test_every_entry_names_a_trace_it_was_seen_in_or_a_public_incident():
    for entry in catalog():
        origin = entry.get("origin", "")
        assert OBSERVED.match(origin) or INCIDENT.match(origin), entry["spec"]


def test_every_entry_has_its_boundary_and_a_description():
    for entry in catalog():
        assert entry["boundary"] == entry["spec"].partition(":")[0], entry["spec"]
        assert entry["description"].strip(), entry["spec"]
