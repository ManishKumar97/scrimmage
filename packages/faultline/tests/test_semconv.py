"""The product's GenAI names match OpenTelemetry's package.

The product writes its own copy so it has no dependencies. OpenTelemetry's package is a
test tool here, like pytest: it is never installed with the product.
"""

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes
from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import (
    GenAiOperationNameValues,
)
from opentelemetry.semconv.attributes import error_attributes

from faultline import semconv


def opentelemetry_value(name: str) -> str | None:
    for module in (gen_ai_attributes, error_attributes):
        if hasattr(module, name):
            return getattr(module, name)
    if name in GenAiOperationNameValues.__members__:
        return GenAiOperationNameValues[name].value
    return None


def test_every_product_name_matches_opentelemetry():
    names = [name for name in vars(semconv) if name.isupper()]

    wrong = {
        name: (getattr(semconv, name), opentelemetry_value(name))
        for name in names
        if getattr(semconv, name) != opentelemetry_value(name)
    }

    assert names
    assert wrong == {}
