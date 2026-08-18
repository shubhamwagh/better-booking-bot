"""Tests for env-based Settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from better_bot.settings import NtfySettings, Settings


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch):
    """Settings reads .env relative to cwd - run from an empty dir so the
    repo's real .env (real credentials) never leaks into these tests."""
    monkeypatch.chdir(tmp_path)


def test_requires_username_and_password(monkeypatch):
    monkeypatch.delenv("BETTER_USERNAME", raising=False)
    monkeypatch.delenv("BETTER_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_reads_credentials_from_env(monkeypatch):
    monkeypatch.setenv("BETTER_USERNAME", "user@example.com")
    monkeypatch.setenv("BETTER_PASSWORD", "secret")
    s = Settings()
    assert s.better_username == "user@example.com"
    assert s.better_password == "secret"
    assert s.card_cvv == ""
    assert s.card_number is None
    assert s.save_card is False


def test_save_card_parses_truthy_strings(monkeypatch):
    monkeypatch.setenv("BETTER_USERNAME", "u")
    monkeypatch.setenv("BETTER_PASSWORD", "p")
    monkeypatch.setenv("SAVE_CARD", "true")
    assert Settings().save_card is True


def test_to_card_maps_all_fields(monkeypatch):
    monkeypatch.setenv("BETTER_USERNAME", "u")
    monkeypatch.setenv("BETTER_PASSWORD", "p")
    monkeypatch.setenv("CARD_CVV", "123")
    monkeypatch.setenv("CARD_NUMBER", "4111111111111111")
    monkeypatch.setenv("CARD_EXPIRY", "12/30")
    monkeypatch.setenv("BILLING_FIRST_NAME", "Ada")
    monkeypatch.setenv("BILLING_LAST_NAME", "Lovelace")
    monkeypatch.setenv("BILLING_POSTCODE", "OX1 1AA")

    card = Settings().to_card()
    assert card.cvv == "123"
    assert card.number == "4111111111111111"
    assert card.expiry == "12/30"
    assert card.first_name == "Ada"
    assert card.last_name == "Lovelace"
    assert card.postcode == "OX1 1AA"


def test_to_card_defaults_cvv_only(monkeypatch):
    monkeypatch.setenv("BETTER_USERNAME", "u")
    monkeypatch.setenv("BETTER_PASSWORD", "p")
    monkeypatch.setenv("CARD_CVV", "999")
    card = Settings().to_card()
    assert card.cvv == "999"
    assert card.number is None


def test_ntfy_settings_all_optional_no_env_needed():
    """Unlike Settings, NtfySettings must never raise just from being
    instantiated - notify.send() calls it unconditionally on every booking
    outcome, including in environments with no BETTER_USERNAME/PASSWORD set."""
    s = NtfySettings()
    assert s.ntfy_url is None
    assert s.ntfy_topic is None
    assert s.ntfy_token is None


def test_ntfy_settings_reads_from_env(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.shublab.com")
    monkeypatch.setenv("NTFY_TOPIC", "booking-alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_abc123")
    s = NtfySettings()
    assert s.ntfy_url == "https://ntfy.shublab.com"
    assert s.ntfy_topic == "booking-alerts"
    assert s.ntfy_token == "tk_abc123"
