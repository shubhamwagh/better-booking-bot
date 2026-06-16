"""Tests for CardDetails model and pure checkout helpers."""

from __future__ import annotations

import pytest

from better_bot.checkout import CardDetails


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
        with pytest.raises(Exception):
            CardDetails()  # cvv is required

    def test_cvv_is_required_field(self):
        assert CardDetails.model_fields["cvv"].is_required()
