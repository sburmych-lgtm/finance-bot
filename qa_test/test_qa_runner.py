import hashlib
import hmac
import json
from urllib.parse import parse_qsl

import pytest

from qa_test import qa_runner
from qa_test import cleanup_leftovers


def test_init_data_is_freshly_signed_without_embedding_secrets():
    token = "unit-test-token"
    raw = qa_runner.make_init_data(
        {"id": 999000123001, "first_name": "QA A", "username": "qa_a"},
        token,
        auth_epoch=1_700_000_000,
    )
    parsed = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = parsed.pop("hash")
    check = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()

    assert parsed["auth_date"] == "1700000000"
    assert json.loads(parsed["user"])["id"] == 999000123001
    assert hmac.compare_digest(
        received_hash,
        hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(),
    )
    assert token not in raw


def test_config_rejects_real_or_duplicate_qa_users():
    base = {
        "API_BASE_URL": "https://worker.example",
        "TELEGRAM_BOT_TOKEN": "token",
        "QA_ADMIN_USER_JSON": json.dumps({"id": 42, "first_name": "Admin"}),
        "ADMIN_IDS": "42",
    }

    with pytest.raises(qa_runner.QaConfigError, match="999000"):
        qa_runner.QaConfig.from_env({
            **base,
            "QA_USER_ID_A": "123",
            "QA_USER_ID_B": "999000123002",
        })
    with pytest.raises(qa_runner.QaConfigError, match="distinct"):
        qa_runner.QaConfig.from_env({
            **base,
            "QA_USER_ID_A": "999000123001",
            "QA_USER_ID_B": "999000123001",
        })


def test_broadcast_confirmation_is_hard_gated_by_explicit_env_flag(monkeypatch):
    monkeypatch.setenv("QA_ALLOW_BROADCAST", "1")
    assert qa_runner.broadcast_confirmation_allowed({}) is False
    assert qa_runner.broadcast_confirmation_allowed({"QA_ALLOW_BROADCAST": "0"}) is False
    assert qa_runner.broadcast_confirmation_allowed({"QA_ALLOW_BROADCAST": "true"}) is True


def test_cleanup_runs_for_both_synthetic_users_after_failure():
    calls = []

    class Runner:
        user_ids = (999000123001, 999000123002)

        def run_all(self):
            raise RuntimeError("boom")

        def delete_account(self, user_id):
            calls.append(user_id)

    with pytest.raises(RuntimeError, match="boom"):
        qa_runner.run_with_cleanup(Runner())

    assert calls == [999000123001, 999000123002]


def test_declared_coverage_contains_every_production_api_route():
    required = {
        ("GET", "/api/health"),
        ("GET", "/api/me"),
        ("GET", "/api/exchange-rates"),
        ("GET", "/api/balance"),
        ("GET", "/api/transactions"),
        ("POST", "/api/transactions"),
        ("PATCH", "/api/transactions/{id}"),
        ("DELETE", "/api/transactions/{id}"),
        ("GET", "/api/quick-templates"),
        ("GET", "/api/reports/monthly"),
        ("GET", "/api/reports/payment-sources"),
        ("GET", "/api/reports/employees"),
        ("GET", "/api/reports/tax"),
        ("GET", "/api/reports/accounting"),
        ("GET", "/api/reports/time"),
        ("GET", "/api/reports/category-breakdown"),
        ("GET", "/api/categories"),
        ("GET", "/api/categories/full"),
        ("POST", "/api/categories"),
        ("PATCH", "/api/categories/{type}/{name}"),
        ("DELETE", "/api/categories/{type}/{name}"),
        ("POST", "/api/categories/{type}/{name}/subcategories"),
        ("DELETE", "/api/categories/{type}/{name}/subcategories/{sub}"),
        ("GET", "/api/employees"),
        ("POST", "/api/employees"),
        ("DELETE", "/api/employees/{name}"),
        ("GET", "/api/time-categories"),
        ("POST", "/api/time-categories"),
        ("DELETE", "/api/time-categories/{name}"),
        ("GET", "/api/time-tracks"),
        ("POST", "/api/time-tracks"),
        ("DELETE", "/api/time-tracks/{id}"),
        ("GET", "/api/budgets"),
        ("PUT", "/api/budgets"),
        ("DELETE", "/api/budgets/{type}/{category}"),
        ("GET", "/api/recurring-operations"),
        ("POST", "/api/recurring-operations"),
        ("PATCH", "/api/recurring-operations/{id}"),
        ("DELETE", "/api/recurring-operations/{id}"),
        ("GET", "/api/recurring-suggestions"),
        ("GET", "/api/insights"),
        ("GET", "/api/digest/weekly"),
        ("GET", "/api/forecast"),
        ("GET", "/api/settings"),
        ("DELETE", "/api/settings"),
        ("PATCH", "/api/settings/tax"),
        ("GET", "/api/settings/notifications"),
        ("PATCH", "/api/settings/notifications"),
        ("DELETE", "/api/account"),
        ("POST", "/api/admin/broadcast"),
        ("GET", "/api/admin/broadcasts"),
        ("GET", "/api/admin/broadcasts/{id}"),
        ("GET", "/api/admin/audit"),
        ("GET", "/api/admin/users"),
    }

    assert required <= qa_runner.ROUTE_COVERAGE


def test_http_client_stops_after_bounded_rate_limit_retries():
    class Response:
        status_code = 429
        headers = {"Retry-After": "0"}

        @staticmethod
        def json():
            return {"detail": "rate limited"}

    class Session:
        def __init__(self):
            self.calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    session = Session()
    config = qa_runner.QaConfig(
        base_url="https://worker.example",
        token="token",
        users=(
            {"id": 999000123001, "first_name": "A"},
            {"id": 999000123002, "first_name": "B"},
        ),
        admin_user={"id": 42, "first_name": "Admin"},
        request_delay_seconds=0,
        admin_delay_seconds=0,
    )
    client = qa_runner.ApiClient(
        config, session=session, output=lambda _line: None, sleeper=lambda _seconds: None
    )

    with pytest.raises(qa_runner.QaHttpError, match="after 3 retries"):
        client.request("GET", "/api/me", user=config.users[0])

    assert session.calls == 4


def test_leftover_cleanup_accepts_only_explicit_synthetic_ids():
    assert cleanup_leftovers.parse_user_ids(
        ["999000123001", "999000123002"], {}
    ) == (999000123001, 999000123002)
    assert cleanup_leftovers.parse_user_ids(
        [], {"QA_USER_IDS": "999000123003,999000123004"}
    ) == (999000123003, 999000123004)
    with pytest.raises(qa_runner.QaConfigError, match="999000"):
        cleanup_leftovers.parse_user_ids(["42"], {})
