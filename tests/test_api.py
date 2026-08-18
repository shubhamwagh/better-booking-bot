"""Tests for BetterAPI client and Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest
import respx
from pydantic import ValidationError

from better_bot.api import (
    BetterAPI,
    BetterAPIError,
    CartItem,
    OccurrenceDetails,
    Slot,
)

BASE = "https://better-admin.org.uk/api"


# ------------------------------------------------------------------
# Pydantic model tests
# ------------------------------------------------------------------


class TestSlot:
    def test_basic(self):
        s = Slot(id="abc", starts_at="19:30", status="BOOK", spaces=3, composite_key="ck")
        assert s.id == "abc"
        assert s.spaces == 3

    def test_invalid_missing_field(self):
        # missing composite_key
        with pytest.raises(ValidationError):
            Slot(id="x", starts_at="10:00", status="BOOK", spaces=2)  # ty: ignore[missing-argument]


class TestOccurrenceDetails:
    def test_basic(self):
        o = OccurrenceDetails(ticket_id="t1", pricing_option_id=42)
        assert o.pricing_option_id == 42


class TestCartItem:
    def test_basic(self):
        c = CartItem(cart_item_id=1, name="Pickleball", price_pence=600)
        assert c.price_pence == 600


# ------------------------------------------------------------------
# BetterAPI tests (mocked httpx via respx)
# ------------------------------------------------------------------


@respx.mock
def test_login_sets_token():
    respx.post(f"{BASE}/auth/customer/login").mock(return_value=httpx.Response(200, json={"token": "tok123"}))
    api = BetterAPI()
    api.login("user@example.com", "pass")
    assert api._token == "tok123"
    assert api._client.headers["Authorization"] == "Bearer tok123"
    api.close()


@respx.mock
def test_login_401_raises():
    respx.post(f"{BASE}/auth/customer/login").mock(return_value=httpx.Response(401, json={"message": "Unauthorized"}))
    api = BetterAPI()
    with pytest.raises(BetterAPIError) as exc_info:
        api.login("bad@example.com", "wrong")
    assert exc_info.value.status == 401
    api.close()


@respx.mock
def test_fetch_membership_user_id():
    respx.get(f"{BASE}/auth/user").mock(
        return_value=httpx.Response(200, json={"data": {"membership_user": {"id": 99}}})
    )
    api = BetterAPI()
    uid = api.fetch_membership_user_id()
    assert uid == 99
    assert api.membership_user_id == 99
    api.close()


@respx.mock
def test_get_slots_returns_parsed_slots():
    payload = {
        "data": [
            {
                "id": "slot-1",
                "starts_at": {"format_24_hour": "19:30"},
                "action_to_show": {"status": "BOOK"},
                "spaces_remaining": 5,
                "composite_key": "ck-1",
            },
            {
                "id": "slot-2",
                "starts_at": {"format_24_hour": "20:00"},
                "action_to_show": {"status": "FULL"},
                "spaces_remaining": 0,
                "composite_key": "ck-2",
            },
        ]
    }
    from datetime import date

    respx.get(f"{BASE}/activities/venue/venue-a/activity/act-b/v2/times").mock(
        return_value=httpx.Response(200, json=payload)
    )
    api = BetterAPI()
    slots = api.get_slots("venue-a", "act-b", date(2026, 6, 21))
    assert len(slots) == 2
    assert slots[0].id == "slot-1"
    assert slots[0].status == "BOOK"
    assert slots[1].status == "FULL"
    api.close()


@respx.mock
def test_get_slots_keeps_unreleased_entry_with_null_status():
    """A listed-but-unreleased session has a null status and a usable slot id.

    Better publishes the session minutes before it opens; that entry is what
    pre-arming keys off, so it must survive parsing rather than be discarded.
    """
    payload = {
        "data": [
            {
                "id": "slot-1",
                "starts_at": {"format_24_hour": "19:30"},
                "action_to_show": {"status": None},
                "spaces_remaining": None,
                "composite_key": "ck-1",
            },
            {
                "id": "slot-2",
                "starts_at": {"format_24_hour": "20:00"},
                "action_to_show": {"status": "BOOK"},
                "spaces_remaining": 3,
                "composite_key": "ck-2",
            },
        ]
    }
    from datetime import date

    respx.get(f"{BASE}/activities/venue/venue-a/activity/act-b/v2/times").mock(
        return_value=httpx.Response(200, json=payload)
    )
    api = BetterAPI()
    slots = api.get_slots("venue-a", "act-b", date(2026, 6, 21))
    assert len(slots) == 2
    unreleased, open_slot = slots
    assert unreleased.id == "slot-1"
    assert unreleased.status is None
    assert unreleased.spaces == 0
    assert not unreleased.bookable
    assert open_slot.bookable
    api.close()


@respx.mock
def test_get_slots_skips_malformed_entry():
    payload = {
        "data": [
            {
                # no id at all - genuinely unparseable, unlike a null status
                "starts_at": {"format_24_hour": "19:30"},
                "action_to_show": {"status": "BOOK"},
                "spaces_remaining": 5,
                "composite_key": "ck-1",
            },
            {
                "id": "slot-2",
                "starts_at": {"format_24_hour": "20:00"},
                "action_to_show": {"status": "BOOK"},
                "spaces_remaining": 3,
                "composite_key": "ck-2",
            },
        ]
    }
    from datetime import date

    respx.get(f"{BASE}/activities/venue/venue-a/activity/act-b/v2/times").mock(
        return_value=httpx.Response(200, json=payload)
    )
    api = BetterAPI()
    slots = api.get_slots("venue-a", "act-b", date(2026, 6, 21))
    assert len(slots) == 1
    assert slots[0].id == "slot-2"
    api.close()


@respx.mock
def test_get_slots_422_returns_empty():
    from datetime import date

    respx.get(f"{BASE}/activities/venue/v/activity/a/v2/times").mock(
        return_value=httpx.Response(422, json={"message": "Not yet released"})
    )
    api = BetterAPI()
    slots = api.get_slots("v", "a", date(2026, 6, 30))
    assert slots == []
    api.close()


@respx.mock
def test_get_occurrence_details():
    respx.get(f"{BASE}/v1/activities/occurrences/slot-1").mock(
        return_value=httpx.Response(200, json={"data": {"tickets": [{"id": "t99", "pricing_option": {"id": 7}}]}})
    )
    api = BetterAPI()
    occ = api.get_occurrence_details("slot-1")
    assert occ.ticket_id == "t99"
    assert occ.pricing_option_id == 7
    api.close()


@respx.mock
def test_cart_add_returns_cart_item():
    respx.get(f"{BASE}/auth/user").mock(return_value=httpx.Response(200, json={"data": {"membership_user": {"id": 1}}}))
    respx.post(f"{BASE}/activities/cart/add").mock(
        return_value=httpx.Response(
            200, json={"data": {"items": [{"id": 55, "name": "Pickleball Drop-in", "price": {"raw": 600}}]}}
        )
    )
    api = BetterAPI()
    api.fetch_membership_user_id()
    slot = Slot(id="s1", starts_at="19:30", status="BOOK", spaces=2, composite_key="ck")
    occ = OccurrenceDetails(ticket_id="t1", pricing_option_id=3)
    item = api.cart_add(slot, occ)
    assert item.cart_item_id == 55
    assert item.price_pence == 600
    api.close()


@respx.mock
def test_cart_add_without_membership_raises():
    api = BetterAPI()
    slot = Slot(id="s1", starts_at="19:30", status="BOOK", spaces=2, composite_key="ck")
    occ = OccurrenceDetails(ticket_id="t1", pricing_option_id=3)
    with pytest.raises(RuntimeError, match="fetch_membership_user_id"):
        api.cart_add(slot, occ)
    api.close()


@respx.mock
def test_handle_500_raises():
    respx.get(f"{BASE}/auth/user").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    api = BetterAPI()
    with pytest.raises(BetterAPIError) as exc_info:
        api.fetch_membership_user_id()
    assert exc_info.value.status == 500
    api.close()


def test_context_manager_closes():
    with BetterAPI() as api:
        assert api._client is not None


# ------------------------------------------------------------------
# BetterAPIError.retryable - what the release-time strike hammers through
# ------------------------------------------------------------------


def test_409_contention_is_retryable():
    exc = BetterAPIError(
        409,
        "Sorry, a lot of people are trying to book at the moment and we were unable to process your request. "
        "Please try again.",
    )
    assert exc.retryable


def test_409_full_session_is_final():
    assert not BetterAPIError(409, "The session being booked is already full").retryable


def test_422_not_yet_released_is_retryable():
    assert BetterAPIError(422, "The date should be within the valid days you are able to view.").retryable


def test_server_errors_and_rate_limits_are_retryable():
    assert BetterAPIError(500, "boom").retryable
    assert BetterAPIError(503, "unavailable").retryable
    assert BetterAPIError(429, "slow down").retryable


def test_auth_failure_is_not_retryable():
    assert not BetterAPIError(401, "Unauthorized").retryable


# ------------------------------------------------------------------
# Pre-built cart payload
# ------------------------------------------------------------------


def test_build_cart_payload_matches_cart_add_body():
    api = BetterAPI()
    api.membership_user_id = 999
    slot = Slot(id="slot-1", starts_at="19:30", status=None, spaces=0, composite_key="ck")
    occ = OccurrenceDetails(ticket_id="t1", pricing_option_id=8329)

    payload = api.build_cart_payload(slot, occ)

    assert payload["membership_user_id"] == 999
    assert payload["items"][0]["id"] == "slot-1"
    assert payload["items"][0]["ticket_id"] == "t1"
    assert payload["items"][0]["pricing_option_id"] == 8329
    api.close()


def test_build_cart_payload_without_membership_raises():
    api = BetterAPI()
    slot = Slot(id="slot-1", starts_at="19:30", status=None, spaces=0, composite_key="ck")
    with pytest.raises(RuntimeError):
        api.build_cart_payload(slot, OccurrenceDetails(ticket_id="t1", pricing_option_id=1))
    api.close()


@respx.mock
def test_server_clock_offset_reads_date_header():
    respx.get(f"{BASE}/auth/user").mock(
        return_value=httpx.Response(200, json={}, headers={"date": "Mon, 17 Aug 2026 20:00:00 GMT"})
    )
    api = BetterAPI()
    with patch("better_bot.api.time.time", side_effect=[100.0, 100.4] * 3):
        offset = api.server_clock_offset(samples=3)
    # Server said 2026-08-17T20:00:00Z; local mid-request was t=100.2
    assert offset == pytest.approx(datetime(2026, 8, 17, 20, 0, tzinfo=UTC).timestamp() - 100.2)
    api.close()


@respx.mock
def test_server_clock_offset_falls_back_to_zero():
    respx.get(f"{BASE}/auth/user").mock(return_value=httpx.Response(500, text="boom"))
    api = BetterAPI()
    assert api.server_clock_offset(samples=2) == 0.0
    api.close()
