"""The exit codes and message lists the scrimmage command and the runner share.

Standard library only, so the command can name them without importing the runner.
"""

OK, ROW_FAILED, MISTAKE, INFRA = 0, 1, 2, 3
INFRA_ERROR = "infrastructure error"  # a run's outcome when a provider or Scrimmage failed
# The lists a report's messages go in.
MISTAKES, NOTES, WARNINGS, INFRA_ERRORS = "mistakes", "notes", "warnings", "infra"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


OUTCOMES = (
    "answered",
    "crashed",
    "timed out",
    "refused call",
    "budget reached",
    "not reserved",
    INFRA_ERROR,
    "stopped",
)
PREVIEW = 200  # how much of a value, or of an answer, a record or a document keeps
