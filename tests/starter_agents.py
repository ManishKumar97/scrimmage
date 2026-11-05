"""Function agents of other shapes, for the starter's tests."""


def as_dict(question: str) -> dict[str, str]:
    return {"reply": question.upper()}


async def as_async(question: str) -> str:
    return f"async: {question}"
