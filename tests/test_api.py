"""Tests for BetterAPI client and Pydantic models."""

from __future__ import annotations

import pytest
import httpx
import respx

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
        with pytest.raises(Exception):
            Slot(id="x", starts_at="10:00", status="BOOK", spaces=2)  # missing composite_key


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
    respx.post(f"{BASE}/auth/customer/login").mock(
        return_value=httpx.Response(200, json={"token": "tok123"})
    )
    api = BetterAPI()
    api.login("user@example.com", "pass")
    assert api._token == "tok123"
    assert api._client.headers["Authorization"] == "Bearer tok123"
    api.close()


@respx.mock
def test_login_401_raises():
    respx.post(f"{BASE}/auth/customer/login").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
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
        return_value=httpx.Response(200, json={
            "data": {
                "tickets": [{"id": "t99", "pricing_option": {"id": 7}}]
            }
        })
    )
    api = BetterAPI()
    occ = api.get_occurrence_details("slot-1")
    assert occ.ticket_id == "t99"
    assert occ.pricing_option_id == 7
    api.close()


@respx.mock
def test_cart_add_returns_cart_item():
    respx.get(f"{BASE}/auth/user").mock(
        return_value=httpx.Response(200, json={"data": {"membership_user": {"id": 1}}})
    )
    respx.post(f"{BASE}/activities/cart/add").mock(
        return_value=httpx.Response(200, json={
            "data": {
                "items": [{"id": 55, "name": "Pickleball Drop-in", "price": {"raw": 600}}]
            }
        })
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
    respx.get(f"{BASE}/auth/user").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    api = BetterAPI()
    with pytest.raises(BetterAPIError) as exc_info:
        api.fetch_membership_user_id()
    assert exc_info.value.status == 500
    api.close()


def test_context_manager_closes():
    with BetterAPI() as api:
        assert api._client is not None
