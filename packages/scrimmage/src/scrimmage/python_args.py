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


def separate_command(args: list[str]) -> int | None:
    """Where the code is when Python runs it with `-c` as a word of its own, after options
    only; None if Python runs anything else first: a script, a module, or code joined to
    its `-c` (`-cCODE`) or to another flag."""
    at = 0
    while at < len(args):
        word = args[at]
        if word == LONG_WITH_VALUE:
            at += 2
            continue
        if not word.startswith("-") or word in ("-", "--"):
            return None
        if not word.startswith("--"):
            for place, letter in enumerate(word[1:], 2):
                if letter in "cm":
                    return at + 1 if word == "-c" and at + 1 < len(args) else None
                if letter in "WX":
                    if place == len(word):
                        at += 1  # its value is the next word
                    break
        at += 1
    return None
