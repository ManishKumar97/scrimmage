"""How a provider opens and builds the opaque Fields and Reply.

Fields and Reply hide their value from every module outside the providers. This is the one
place that reads it, and the import contract forbids every module but the providers from
importing it."""
# pyright: reportPrivateUsage=false

from scrimmage.providers.base import Fields, Reply


def fields_dict(fields: Fields | None) -> dict[str, object]:
    """The request's JSON object, for a provider to read. Empty for None."""
    return {} if fields is None else fields._value


def wrap_fields(value: dict[str, object]) -> Fields:
    """A provider's own dict, as a request's Fields to hand on."""
    return Fields(value)


def reply_value(reply: Reply) -> object:
    """The parsed reply, for a provider to read."""
    return reply._value
