"""Better (GLL) API client.

All network calls to better-admin.org.uk live here.
No business logic - just raw API wrappers.
"""

from __future__ import annotations

import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel

BASE_URL = "https://better-admin.org.uk/api"
ORIGIN = "https://bookings.better.org.uk"

# Enough pooled connections for the release-time strike burst plus headroom.
MAX_CONNECTIONS = 8

log = logging.getLogger(__name__)


class Slot(BaseModel):
    id: str
    starts_at: str  # "HH:MM" 24-hour
    status: str | None  # "BOOK" | "FULL" | None (listed but not yet released)
    spaces: int
    composite_key: str
    booking_id: int | None = None  # set when the logged-in user already holds this slot

    @property
    def bookable(self) -> bool:
        return self.status == "BOOK" and self.spaces > 0


class OccurrenceDetails(BaseModel):
    ticket_id: str
    pricing_option_id: int


class CartItem(BaseModel):
    cart_item_id: int
    name: str
    price_pence: int


class Venue(BaseModel):
    slug: str
    name: str
    town: str


class Activity(BaseModel):
    slug: str
    name: str
    category: str  # top-level category name, e.g. "Pickleball"


class BetterAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        """True for load-shedding / not-yet-open responses worth hammering through.

        At release time Better returns 409 with "a lot of people are trying to
        book at the moment ... Please try again" - that is backpressure, not a
        refusal, and the slot may still have spaces. A 409 that actually says
        the session is full is final.
        """
        if self.status in (429, 425) or self.status >= 500:
            return True
        if self.status in (409, 422):
            return "full" not in self.message.lower()
        return False


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
            timeout=httpx.Timeout(15.0, connect=5.0),
            # Release time is a race: keep every connection alive and idle-ready
            # so no strike attempt pays for a TCP + TLS handshake.
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_CONNECTIONS,
                keepalive_expiry=120.0,
            ),
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
    # Venue / activity discovery (no auth required)
    # ------------------------------------------------------------------

    def list_venues(self) -> list[Venue]:
        resp = self._get("/activities/venues")
        return [Venue(slug=v["slug"], name=v["name"], town=v["town"]) for v in resp.get("data", [])]

    def list_activities(self, venue_slug: str) -> list[Activity]:
        resp = self._get(f"/activities/venue/{venue_slug}/categories")
        activities: list[Activity] = []
        for cat in resp.get("data", []):
            if cat["has_children"]:
                activities.extend(self._category_leaves(venue_slug, cat["slug"], cat["name"]))
            else:
                activities.append(Activity(slug=cat["slug"], name=cat["name"], category=cat["name"]))
        return activities

    def _category_leaves(self, venue_slug: str, category_slug: str, category_name: str) -> list[Activity]:
        resp = self._get(f"/activities/venue/{venue_slug}/categories/{category_slug}")
        leaves: list[Activity] = []
        for child in resp["data"].get("children", []):
            if child["has_children"]:
                leaves.extend(self._category_leaves(venue_slug, child["slug"], category_name))
            else:
                leaves.append(Activity(slug=child["slug"], name=child["name"], category=category_name))
        return leaves

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
            try:
                slots.append(
                    Slot(
                        id=t["id"],
                        starts_at=t["starts_at"]["format_24_hour"],
                        status=(t.get("action_to_show") or {}).get("status"),
                        spaces=t.get("spaces_remaining") or 0,
                        composite_key=t["composite_key"],
                        booking_id=(t.get("booking") or {}).get("id"),
                    )
                )
            except Exception as exc:
                # One malformed entry (e.g. a null status) shouldn't crash
                # the whole poll - skip it and keep looking at the rest.
                log.warning("Skipping malformed slot entry %r: %s", t.get("composite_key"), exc)
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

    def build_cart_payload(self, slot: Slot, occurrence: OccurrenceDetails) -> dict[str, Any]:
        """The exact /activities/cart/add body, built ahead of release time.

        Split out from cart_add so the release-time strike has nothing left to
        compute or look up - just a POST of an already-serialised payload.
        """
        if self.membership_user_id is None:
            raise RuntimeError("Call fetch_membership_user_id() before build_cart_payload()")
        return {
            "items": [
                {
                    "id": slot.id,
                    "type": "purchasableOccurrence",
                    "purchased_for_user_id": None,
                    "pricing_option_id": occurrence.pricing_option_id,
                    "ticket_id": occurrence.ticket_id,
                    "activity_restriction_ids": [],
                }
            ],
            "membership_user_id": self.membership_user_id,
            "selected_user_id": None,
        }

    def cart_add(self, slot: Slot, occurrence: OccurrenceDetails) -> CartItem:
        return self.cart_add_prepared(self.build_cart_payload(slot, occurrence))

    def cart_add_prepared(self, payload: dict[str, Any]) -> CartItem:
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
        self._post(
            "/activities/cart/remove",
            {
                "cart_item_ids": [cart_item_id],
                "membership_user_id": self.membership_user_id,
                "selected_user_id": None,
            },
        )

    def get_cart(self) -> dict[str, Any]:
        return self._get("/activities/cart")["data"]

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_booking(self, booking_id: int) -> None:
        """Cancel a confirmed booking (same call the My Account site makes)."""
        self._patch(
            "/v1/activities/bookings",
            {
                "data": {
                    "cancellation_source": "my-account",
                    "update_type": "cancellation",
                    "booking_ids": [booking_id],
                },
            },
        )

    # ------------------------------------------------------------------
    # Checkout prepare (returns Opayo session key)
    # ------------------------------------------------------------------

    def checkout_prepare(self) -> dict[str, Any]:
        resp = self._get("/checkout/prepare")
        log.debug("Checkout prepare: provider=%s", resp.get("payment_provider"))
        return resp

    # ------------------------------------------------------------------
    # Release-time preparation
    # ------------------------------------------------------------------

    def warm_connections(self, count: int = 3) -> None:
        """Open and keep alive `count` pooled connections.

        Fired a few seconds before release so the strike burst finds warm
        sockets instead of paying TCP + TLS setup per attempt.
        """

        def ping() -> None:
            try:
                self._client.get("/auth/user")
            except Exception as exc:
                log.debug("Connection warm-up ping failed: %s", exc)

        with ThreadPoolExecutor(max_workers=count) as pool:
            for _ in range(count):
                pool.submit(ping)
        log.debug("Warmed %d connections", count)

    def server_clock_offset(self, samples: int = 3) -> float:
        """Seconds to add to local time to get server time.

        Read from the `Date` response header, which is only second-granular -
        so this is accurate to roughly ±0.5s. That is deliberately good enough:
        the strike starts slightly *before* the computed release instant and
        retries through it, so a small offset error costs nothing.
        """
        deltas = []
        for _ in range(samples):
            try:
                t0 = time.time()
                r = self._client.get("/auth/user")
                t1 = time.time()
                server = parsedate_to_datetime(r.headers["date"]).timestamp()
                deltas.append(server - (t0 + t1) / 2)
            except Exception as exc:
                log.debug("Clock sample failed: %s", exc)
        if not deltas:
            log.warning("Could not read server clock - assuming no offset")
            return 0.0
        offset = statistics.median(deltas)
        log.info("Server clock offset: %+.2fs", offset)
        return offset

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._client.get(path, params=params)
        return self._handle(r)

    def _post(self, path: str, body: Any) -> Any:
        r = self._client.post(path, json=body)
        return self._handle(r)

    def _patch(self, path: str, body: Any) -> Any:
        r = self._client.patch(path, json=body)
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
