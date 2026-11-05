from collections.abc import Callable, Iterator

import pytest

from fakes.openai_server import FakeOpenAI


@pytest.fixture
def fake_openai() -> Iterator[Callable[[list[str]], FakeOpenAI]]:
    """Start a fake OpenAI API that serves the given fixtures in order.

    At the end of the test it fails if the fake answered any request with an error or
    if a listed fixture was never asked for.
    """
    started: list[FakeOpenAI] = []

    def start(replies: list[str]) -> FakeOpenAI:
        fake = FakeOpenAI(replies)
        fake.start()
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.stop()
        assert not fake.errors, (
            f"the fake OpenAI API got requests it had no reply for: {fake.errors}"
        )
        assert not fake.unused, f"fixtures listed but never asked for: {fake.unused}"
