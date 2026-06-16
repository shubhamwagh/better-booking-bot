"""Better (GLL) API client.

All network calls to better-admin.org.uk live here.
No business logic — just raw API wrappers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

BASE_URL = "https://better-admin.org.uk/api"
ORIGIN = "https://bookings.better.org.uk"

log = logging.getLogger(__name__)


@dataclass
class Slot:
    id: str
    starts_at: str  # "HH:MM" 24-hour
    status: str     # "BOOK" | "FULL" | ...
    spaces: int
    composite_key: str


@dataclass
class OccurrenceDetails:
    ticket_id: str
    pricing_option_id: int


@dataclass
class CartItem:
    cart_item_id: int
    name: str
    price_pence: int


class BetterAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class BetterAPI:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Origin": ORIGIN,
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            },
            timeout=15.0,
        )
        self._token: str | None = None
        self.membership_user_id: int | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        resp = self._post("/auth/customer/login", {"username": username, "password": password})
        self._token = resp["token"]
        self._client.headers["Authorization"] = f"Bearer {self._token}"
        log.info("Logged in as %s", username)

    def fetch_membership_user_id(self) -> int:
        resp = self._get("/auth/user")
        self.membership_user_id = resp["data"]["membership_user"]["id"]
        log.debug("membership_user_id=%s", self.membership_user_id)
        return self.membership_user_id

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def get_slots(self, venue_slug: str, activity_slug: str, target_date: date) -> list[Slot]:
        path = f"/activities/venue/{venue_slug}/activity/{activity_slug}/v2/times"
        try:
            resp = self._get(path, params={"date": target_date.isoformat()})
        except BetterAPIError as exc:
            if exc.status == 422:
                # Slots not yet released for this date
                log.debug("Slots for %s not yet available (422)", target_date)
                return []
            raise
        slots = []
        for t in resp.get("data", []):
            slots.append(Slot(
                id=t["id"],
                starts_at=t["starts_at"]["format_24_hour"],
                status=t["action_to_show"]["status"],
                spaces=t.get("spaces_remaining", 0),
                composite_key=t["composite_key"],
            ))
        return slots

    # ------------------------------------------------------------------
    # Occurrence details (ticket_id + pricing_option_id)
    # ------------------------------------------------------------------

    def get_occurrence_details(self, slot_id: str) -> OccurrenceDetails:
        resp = self._get(f"/v1/activities/occurrences/{slot_id}")
        ticket = resp["data"]["tickets"][0]
        return OccurrenceDetails(
            ticket_id=ticket["id"],
            pricing_option_id=ticket["pricing_option"]["id"],
        )

    # ------------------------------------------------------------------
    # Cart
    # ------------------------------------------------------------------

    def cart_add(self, slot: Slot, occurrence: OccurrenceDetails) -> CartItem:
        if self.membership_user_id is None:
            raise RuntimeError("Call fetch_membership_user_id() before cart_add()")
        payload = {
            "items": [{
                "id": slot.id,
                "type": "purchasableOccurrence",
                "purchased_for_user_id": None,
                "pricing_option_id": occurrence.pricing_option_id,
                "ticket_id": occurrence.ticket_id,
                "activity_restriction_ids": [],
            }],
            "membership_user_id": self.membership_user_id,
            "selected_user_id": None,
        }
        resp = self._post("/activities/cart/add", payload)
        items = resp["data"]["items"]
        if not items:
            raise BetterAPIError(200, "Cart add succeeded but no items returned")
        item = items[0]
        return CartItem(
            cart_item_id=item["id"],
            name=item["name"],
            price_pence=item["price"]["raw"],
        )

    def cart_remove(self, cart_item_id: int) -> None:
        if self.membership_user_id is None:
            raise RuntimeError("Call fetch_membership_user_id() before cart_remove()")
        self._post("/activities/cart/remove", {
            "cart_item_ids": [cart_item_id],
            "membership_user_id": self.membership_user_id,
            "selected_user_id": None,
        })

    def get_cart(self) -> dict[str, Any]:
        return self._get("/activities/cart")["data"]

    # ------------------------------------------------------------------
    # Checkout prepare (returns Opayo session key)
    # ------------------------------------------------------------------

    def checkout_prepare(self) -> dict[str, Any]:
        resp = self._get("/checkout/prepare")
        log.debug("Checkout prepare: provider=%s", resp.get("payment_provider"))
        return resp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._client.get(path, params=params)
        return self._handle(r)

    def _post(self, path: str, body: Any) -> Any:
        r = self._client.post(path, json=body)
        return self._handle(r)

    @staticmethod
    def _handle(r: httpx.Response) -> Any:
        if r.status_code >= 500:
            raise BetterAPIError(r.status_code, r.text[:200])
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text[:200])
            except Exception:
                msg = r.text[:200]
            raise BetterAPIError(r.status_code, msg)
        try:
            return r.json()
        except Exception:
            return {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BetterAPI:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
