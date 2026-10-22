"""incidents.yaml: which fault each incident turns on and which service is to blame."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The catalog sits at the repository root, three levels above this file.
CATALOG_PATH = Path(__file__).resolve().parents[3] / "incidents.yaml"

_NAME = r"^[a-z][a-z0-9_]*$"
_SERVICE = r"^[a-z][a-z0-9-]*$"


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    flag: str = Field(min_length=1)  # the flagd feature flag that turns the fault on
    variant: str = Field(min_length=1)  # the value AIOpsLab sets it to
    service: str = Field(pattern=_SERVICE)  # AIOpsLab's faulty_service: the answer key
    source: str = Field(pattern=r"^aiopslab/.+\.py$")  # file in the AIOpsLab repository
    # Shown in the summary when the key and the prompt's root-cause definition differ.
    note: str = ""


class Control(BaseModel):
    """A run with no fault. The correct answer is that nothing is wrong."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    source: str = Field(pattern=r"^aiopslab/.+\.py$")


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incidents: list[Incident] = Field(min_length=1)
    control: Control

    @model_validator(mode="after")
    def no_duplicates(self) -> Self:
        names = [i.name for i in self.incidents] + [self.control.name]
        flags = [i.flag for i in self.incidents]
        for label, values in (("name", names), ("flag", flags)):
            seen = {v for v in values if values.count(v) > 1}
            if seen:
                raise ValueError(f"duplicate incident {label}: {sorted(seen)}")
        return self

    def notes(self) -> dict[str, str]:
        """Incident name -> caveat, for the incidents that have one."""
        return {i.name: i.note for i in self.incidents if i.note}

    def get(self, name: str) -> Incident:
        for incident in self.incidents:
            if incident.name == name:
                return incident
        raise KeyError(f"no incident named {name!r}")


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    """Read and validate the catalog. Raise ValueError that names the file."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{path} is invalid: {exc}") from exc
