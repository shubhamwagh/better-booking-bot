"""Tests for the ntfy notification helper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from better_bot import notify


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch):
    """notify.send() reads NtfySettings from .env relative to cwd - run from
    an empty dir so the repo's real .env never leaks into these tests."""
    monkeypatch.chdir(tmp_path)


def _configure_ntfy(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.shublab.com")
    monkeypatch.setenv("NTFY_TOPIC", "booking-alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_abc123")


def test_send_skips_push_when_unconfigured():
    with patch("better_bot.notify.httpx.post") as post:
        notify.send(subject="Booked: Test", body="details")
    assert not post.called


def test_send_posts_to_configured_topic(monkeypatch):
    _configure_ntfy(monkeypatch)
    with patch("better_bot.notify.httpx.post") as post:
        post.return_value = MagicMock(raise_for_status=MagicMock())
        notify.send(subject="Booked: Test", body="details", tags="tada", priority="high", click="https://x/y")

    assert post.called
    url, kwargs = post.call_args[0][0], post.call_args[1]
    assert url == "https://ntfy.shublab.com/booking-alerts"
    assert kwargs["content"] == b"details"
    assert kwargs["headers"]["Authorization"] == "Bearer tk_abc123"
    assert kwargs["headers"]["Title"] == "Booked: Test"
    assert kwargs["headers"]["Priority"] == "high"
    assert kwargs["headers"]["Tags"] == "tada"
    assert kwargs["headers"]["Click"] == "https://x/y"


def test_send_omits_optional_headers_when_not_given(monkeypatch):
    _configure_ntfy(monkeypatch)
    with patch("better_bot.notify.httpx.post") as post:
        post.return_value = MagicMock(raise_for_status=MagicMock())
        notify.send(subject="No slot: Test", body="details")

    headers = post.call_args[1]["headers"]
    assert "Tags" not in headers
    assert "Click" not in headers


def test_send_strips_trailing_slash_from_url(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.shublab.com/")
    monkeypatch.setenv("NTFY_TOPIC", "booking-alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_abc123")
    with patch("better_bot.notify.httpx.post") as post:
        post.return_value = MagicMock(raise_for_status=MagicMock())
        notify.send(subject="x", body="y")
    assert post.call_args[0][0] == "https://ntfy.shublab.com/booking-alerts"


def test_send_swallows_network_errors(monkeypatch):
    """A booking must never fail because the phone-notification push failed."""
    _configure_ntfy(monkeypatch)
    with patch("better_bot.notify.httpx.post", side_effect=httpx.ConnectError("down")):
        notify.send(subject="x", body="y")  # must not raise


def test_send_swallows_http_error_status(monkeypatch):
    _configure_ntfy(monkeypatch)
    with patch("better_bot.notify.httpx.post") as post:
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())
        post.return_value = resp
        notify.send(subject="x", body="y")  # must not raise
