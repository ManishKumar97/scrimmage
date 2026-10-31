"""The fault matrix: for each fault, how many runs passed, failed loudly and failed
silently. `faultline test` prints it at the end and writes it to DIR/matrix.json.

One row for the clean runs and one per fault, in the order the faults were given.
A run with no verdict, such as a fault never applied or no reference, counts in its
own column, so every run is in exactly one cell.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from faultline.model import Verdict

CLEAN = "clean"
NO_VERDICT = "no_verdict"
COLUMNS = (*(verdict.value for verdict in Verdict), NO_VERDICT)
HEADINGS = {
    Verdict.PASSED.value: "passed",
    Verdict.FAILED_LOUDLY.value: "failed loudly",
    Verdict.FAILED_SILENTLY.value: "failed silently",
    NO_VERDICT: "no verdict",
}


@dataclass(frozen=True)
class Mark:
    """One run, as the matrix counts it."""

    row: str  # "clean" or the fault spec
    verdict: Verdict | None  # None when the run has no verdict
    directory: str  # the run's directory, relative to the test's DIR


@dataclass(frozen=True)
class Row:
    name: str
    cells: dict[str, list[str]]  # column: the directories of its runs

    @property
    def runs(self) -> int:
        return sum(len(dirs) for dirs in self.cells.values())


def rows(marks: Sequence[Mark], faults: Sequence[str]) -> list[Row]:
    """The clean row, then a row per fault, each with every column."""
    table = [Row(name, {column: [] for column in COLUMNS}) for name in (CLEAN, *faults)]
    by_name = {row.name: row for row in table}
    for mark in marks:
        column = mark.verdict.value if mark.verdict else NO_VERDICT
        by_name[mark.row].cells[column].append(mark.directory)
    return table


def to_json(table: Sequence[Row]) -> dict:
    return {
        "version": 1,
        "columns": [*COLUMNS, "runs"],
        "rows": [
            {
                "fault": row.name,
                **{column: len(row.cells[column]) for column in COLUMNS},
                "runs": row.runs,
                "cells": row.cells,
            }
            for row in table
        ],
    }


def show(table: Sequence[Row]) -> str:
    """The matrix as a text table, one row per line, numbers right-aligned."""
    headings = ["fault", *(HEADINGS[column] for column in COLUMNS), "runs"]
    lines = [
        [row.name, *(str(len(row.cells[column])) for column in COLUMNS), str(row.runs)]
        for row in table
    ]
    widths = [
        max(len(line[i]) for line in [headings, *lines]) for i in range(len(headings))
    ]

    def render(cells: list[str]) -> str:
        first = cells[0].ljust(widths[0])
        rest = [
            cell.rjust(width) for cell, width in zip(cells[1:], widths[1:], strict=True)
        ]
        return "  ".join([first, *rest])

    return "\n".join(render(cells) for cells in [headings, *lines])
