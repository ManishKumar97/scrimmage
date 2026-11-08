"""Reading a command that runs Python: its interpreter and the flags that skip Scrimmage."""

import os
import re
import shutil

PYTHON = re.compile(r"(?:python|pypy)[\d.]*")
SKIPS = set("IES")  # flags that ignore PYTHONPATH or skip the startup file
LONG_WITH_VALUE = "--check-hash-based-pycs"  # the one long flag whose value is the next word


def interpreter(path: str, env: dict[str, str]) -> tuple[str, list[str]] | None:
    """The Python a file runs and the flags its shebang gives, or None if it is not Python."""
    if PYTHON.fullmatch(os.path.basename(path)):
        return path, []
    try:
        with open(path, "rb") as file:
            line = file.readline(256)
    except OSError:
        return None
    words = line[2:].decode(errors="replace").split() if line.startswith(b"#!") else []
    for index, word in enumerate(words):
        if PYTHON.fullmatch(os.path.basename(word)):
            return shutil.which(word, path=env.get("PATH")) or word, words[index + 1 :]
    return None


def flags(args: list[str]) -> set[str]:
    """The single-letter flags before the program Python is told to run."""
    found: set[str] = set()
    words = iter(args)
    for word in words:
        if word == LONG_WITH_VALUE:
            next(words, None)
            continue
        if not word.startswith("-") or word in ("-", "--"):
            break
        for place, letter in enumerate("" if word.startswith("--") else word[1:], 2):
            if letter in "cm":
                return found
            if letter in "WX":
                if place == len(word):
                    next(words, None)
                break
            found.add(letter)
    return found


def skipping(args: list[str]) -> str | None:
    """The first flag that keeps Python from running Scrimmage's startup file."""
    skipped = sorted(flags(args) & SKIPS)
    return f"-{skipped[0]}" if skipped else None
