"""Failing contracts for the remaining Block 0 release blockers.

These tests intentionally describe the required production behavior before
the implementation exists. They must not be weakened to preserve the current
unsafe fallback paths.
"""

import asyncio
import json
from datetime import datetime

import pytest

import bot


def run(coro):
    return asyncio.run(coro)


def response_payload(response):
    return json.loads(response.body)


def test_current_tax_rules_year_follows_kyiv_calendar_not_max_configured(monkeypatch):
    future_rules = {
        **bot.TAX_RULES_BY_YEAR,
        2027: dict(bot.TAX_RULES_BY_YEAR[2026]),
    }
    monkeypatch.setattr(bot, "TAX_RULES_BY_YEAR", future_rules)

    selected = bot.current_tax_rules_year(
        now=datetime(2026, 12, 15, 12, 0, tzinfo=bot.KYIV_TZ)
    )

    assert selected == 2026


def test_current_tax_rules_year_fails_closed_when_calendar_year_is_unsupported(
    monkeypatch,
):
    monkeypatch.setattr(
        bot,
        "TAX_RULES_BY_YEAR",
        {2025: dict(bot.TAX_RULES_BY_YEAR[2025])},
    )

    with pytest.raises(ValueError, match="tax rules unavailable for 2026"):
        bot.current_tax_rules_year(
            now=datetime(2026, 1, 1, 0, 1, tzinfo=bot.KYIV_TZ)
        )


@pytest.mark.parametrize("raw", ["invalid", "NaN", "Infinity", "-0.1", "1.1"])
def test_invalid_sentry_trace_sample_rate_falls_back_without_crashing(raw):
    assert bot.parse_sentry_traces_sample_rate(raw, default=0.0) == 0.0


def test_sentry_event_sanitizer_removes_telegram_credential_and_request_body():
    event = {
        "request": {
            "url": "https://worker.example/api/transactions",
            "headers": {
                "X-Telegram-Init-Data": "signed-sensitive-credential",
                "Authorization": "Bearer another-secret",
                "Content-Type": "application/json",
            },
            "data": {
                "amount": 1000,
                "description": "sensitive financial note",
            },
            "cookies": {"session": "secret"},
        },
        "extra": {
            "request_body": {"description": "another sensitive note"},
        },
    }

    sanitized = bot.sanitize_sentry_event(event, hint={})
    request = sanitized["request"]

    assert "X-Telegram-Init-Data" not in request.get("headers", {})
    assert "Authorization" not in request.get("headers", {})
    assert "data" not in request
    assert "cookies" not in request
    assert "request_body" not in sanitized.get("extra", {})
    assert request["headers"]["Content-Type"] == "application/json"


@pytest.mark.parametrize("configured", [False, True])
def test_required_backup_keeps_health_unready_without_verified_backup(
    monkeypatch,
    tmp_path,
    configured,
):
    database = bot.Database(str(tmp_path / "health.db"))
    monkeypatch.setattr(bot, "db", database)
    monkeypatch.setenv("BACKUP_REQUIRED", "1")
    for name in (
        "BACKUP_S3_BUCKET",
        "BACKUP_S3_PREFIX",
        "BACKUP_S3_ENDPOINT_URL",
        "BACKUP_S3_REGION",
        "BACKUP_S3_ACCESS_KEY_ID",
        "BACKUP_S3_SECRET_ACCESS_KEY",
        "BACKUP_S3_SESSION_TOKEN",
        "BACKUP_S3_SSE",
    ):
        monkeypatch.delenv(name, raising=False)
    if configured:
        monkeypatch.setenv("BACKUP_S3_BUCKET", "ruby-finance-backups")

    monkeypatch.setattr(
        bot,
        "backup_status",
        {
            "last_success": None,
            "last_error": None,
            "last_remote_key": None,
            "last_checksum": None,
        },
    )

    response = run(bot.api_health(None))
    payload = response_payload(response)

    assert response.status == 503
    assert payload["ok"] is False
    assert payload["backup"]["required"] is True
    assert payload["backup"]["ready"] is False
