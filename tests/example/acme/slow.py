"""Agents that misbehave on purpose: one hangs, one crashes."""

import time


def hang(question: str) -> str:
    time.sleep(600)
    return "never"


def crash(question: str) -> str:
    raise RuntimeError("the bot crashed")
