"""A file of the team's named scrimmage, found on the agent's path ahead of this package."""

import os
from importlib.machinery import PathFinder
from pathlib import Path

import scrimmage


def problem(root: Path) -> str | None:
    """Why the agent would import another scrimmage than this one, or None.

    Its PYTHONPATH comes before the installed packages, so a scrimmage in one of its folders
    wins. The name is looked up over those folders as the import system would, without
    importing it. The folder the agent starts in is not looked up: it is kept off the path
    (python -P) while Scrimmage's own modules load, and scrimmage is imported by then.
    A folder named scrimmage with no __init__.py is a namespace: a package found later wins.
    """
    given = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    folders = [os.path.join(root, p) for p in given if p]  # a relative one means from root
    spec = PathFinder.find_spec("scrimmage", folders)
    if spec is None or spec.origin is None:  # nothing found, or a namespace folder
        return None
    mine = scrimmage.__file__
    if mine is not None and os.path.samefile(spec.origin, mine):
        return None
    return (
        f"{spec.origin} would be imported as scrimmage by the agent, in place of Scrimmage. "
        "Rename it, or remove its folder from PYTHONPATH"
    )
