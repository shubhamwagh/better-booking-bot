"""Tests for CardDetails model and pure checkout helpers."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from better_bot.checkout import CardDetails, Deadline, PaymentAmbiguousError


class TestCardDetails:
    def test_minimal_saved_card(self):
        c = CardDetails(cvv="123")
        assert c.cvv == "123"
        assert c.number is None
        assert c.save_card is False

    def test_full_new_card(self):
        c = CardDetails(
            cvv="321",
            number="4111111111111111",
            expiry="12/27",
            first_name="John",
            last_name="Smith",
            address1="123 High Street",
            city="Oxford",
            postcode="OX1 1AA",
        )
        assert c.number == "4111111111111111"
        assert c.first_name == "John"
        assert c.postcode == "OX1 1AA"

    def test_save_card_default_false(self):
        c = CardDetails(cvv="999")
        assert c.save_card is False

    def test_save_card_true(self):
        c = CardDetails(cvv="999", save_card=True)
        assert c.save_card is True

    def test_optional_fields_none(self):
        c = CardDetails(cvv="123")
        assert c.number is None
        assert c.expiry is None
        assert c.first_name is None
        assert c.last_name is None
        assert c.address1 is None
        assert c.address2 is None
        assert c.city is None
        assert c.postcode is None

    def test_missing_cvv_raises(self):
        # cvv is required
        with pytest.raises(ValidationError):
            CardDetails()  # ty: ignore[missing-argument]

    def test_cvv_is_required_field(self):
        assert CardDetails.model_fields["cvv"].is_required()


class TestDeadline:
    """The shared time budget every checkout step draws its own timeout
    from - see the incident in the module docstring: a single .click() with
    no explicit timeout silently used Playwright's ~30s default and burned
    a third of a whole attempt's budget by itself."""

    def test_remaining_s_counts_down(self):
        d = Deadline(1.0)
        assert 0 < d.remaining_s() <= 1.0

    def test_remaining_ms_capped_below_budget(self):
        d = Deadline(10.0)
        assert d.remaining_ms(cap_ms=500) <= 500

    def test_remaining_ms_never_exceeds_actual_remaining_time(self):
        d = Deadline(0.05)
        time.sleep(0.06)
        # Expired - remaining_ms must not report the full cap regardless.
        assert d.remaining_ms(cap_ms=5_000) <= 300  # falls back to the floor

    def test_remaining_ms_floors_instead_of_zero(self):
        """A 0ms Playwright timeout means 'no timeout' on some calls - the
        opposite of what an expired Deadline should produce. A small floor
        keeps it failing fast without accidentally disabling the timeout."""
        d = Deadline(-5.0)  # already expired
        assert d.remaining_ms(cap_ms=5_000, floor_ms=300) == 300

    def test_expired_true_once_budget_elapses(self):
        d = Deadline(0.01)
        time.sleep(0.02)
        assert d.expired() is True

    def test_expired_false_within_budget(self):
        d = Deadline(10.0)
        assert d.expired() is False


class TestPaymentAmbiguousError:
    def test_is_a_runtime_error(self):
        """Callers that don't specifically check for PaymentAmbiguousError
        (nothing in this codebase should, but a stray `except RuntimeError`
        elsewhere shouldn't silently swallow it either) still see it as an
        error, never as some unrelated exception hierarchy."""
        assert issubclass(PaymentAmbiguousError, RuntimeError)
