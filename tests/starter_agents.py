"""Function agents of other shapes, for the starter's tests."""

import enum


def as_dict(question: str) -> dict[str, str]:
    return {"reply": question.upper()}


async def as_async(question: str) -> str:
    return f"async: {question}"


class Status(str, enum.Enum):  # noqa: UP042  # this older form has str() give the name
    SHIPPED = "still pending, no date"


def as_text_enum(question: str) -> Status:
    return Status.SHIPPED
