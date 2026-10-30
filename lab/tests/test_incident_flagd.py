import asyncio

import pytest
from fake_incidents import FakeApi

from faultline_lab.incidents import flagd
from faultline_lab.incidents.catalog import Incident

CART = Incident(
    name="cart_service_failure",
    flag="cartFailure",
    variant="on",
    service="cart",
    source="aiopslab/x.py",
)
PAYMENT = Incident(
    name="payment_service_failure",
    flag="paymentFailure",
    variant="100%",
    service="payment",
    source="aiopslab/x.py",
)


def flag(api: FakeApi, name: str) -> str:
    return api.flagd.file["flags"][name]["defaultVariant"]


def test_inject_sets_only_that_flag_and_keeps_the_rest_of_the_file():
    api = FakeApi()
    before = api.flagd.file["$schema"]

    asyncio.run(flagd.inject(api.client, PAYMENT))

    assert flag(api, "paymentFailure") == "100%"
    assert flag(api, "cartFailure") == "off"
    assert api.flagd.file["$schema"] == before
    assert api.flagd.file["flags"]["paymentFailure"]["variants"]["50%"] == 0.5


def test_inject_never_touches_anything_but_the_two_routes():
    api = FakeApi()

    asyncio.run(flagd.inject(api.client, CART))

    assert {r for _, r in api.flagd.requests} == {"read-file", "write-to-file"}


def test_a_write_that_does_not_stick_is_an_error():
    api = FakeApi()
    api.flagd.ignore_writes = True

    with pytest.raises(flagd.FlagdError, match="the file says 'off'"):
        asyncio.run(flagd.inject(api.client, CART))


def test_an_unknown_flag_or_variant_is_rejected_before_any_write():
    api = FakeApi()
    ghost = CART.model_copy(update={"flag": "ghostFlag"})
    odd = CART.model_copy(update={"variant": "sideways"})

    with pytest.raises(flagd.FlagdError, match="not in the flag file"):
        asyncio.run(flagd.inject(api.client, ghost))
    with pytest.raises(flagd.FlagdError, match="no variant"):
        asyncio.run(flagd.inject(api.client, odd))
    assert all(method == "GET" for method, _ in api.flagd.requests)


def test_http_errors_and_junk_bodies_become_flagd_errors():
    api = FakeApi()
    api.flagd.status = 500
    with pytest.raises(flagd.FlagdError, match="500"):
        asyncio.run(flagd.read_flags(api.client))

    api = FakeApi()
    api.flagd.body = "<html>"
    with pytest.raises(flagd.FlagdError, match="JSON"):
        asyncio.run(flagd.read_flags(api.client))

    api.flagd.body = '{"nothing": 1}'
    with pytest.raises(flagd.FlagdError, match="no flags"):
        asyncio.run(flagd.read_flags(api.client))


def test_recover_all_turns_off_every_flag_and_reports_what_was_on():
    api = FakeApi()
    asyncio.run(flagd.inject(api.client, CART))
    asyncio.run(flagd.inject(api.client, PAYMENT))

    was_on = asyncio.run(flagd.recover_all(api.client))

    assert was_on == {"cartFailure": "on", "paymentFailure": "100%"}
    assert flagd.flags_on(api.flagd.file) == {}


def test_recover_all_writes_nothing_when_nothing_is_on():
    api = FakeApi()

    assert asyncio.run(flagd.recover_all(api.client)) == {}
    assert all(method == "GET" for method, _ in api.flagd.requests)


def test_recover_all_fails_loudly_when_a_flag_stays_on():
    api = FakeApi()
    asyncio.run(flagd.inject(api.client, CART))
    api.flagd.ignore_writes = True

    with pytest.raises(flagd.FlagdError, match="still on"):
        asyncio.run(flagd.recover_all(api.client))
