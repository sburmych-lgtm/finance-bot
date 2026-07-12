"""Production-safe end-to-end regression runner for the Ruby Finance API.

The runner creates two fresh synthetic Telegram users whose IDs start with
``999000``. It covers every public/protected API group, verifies tenant
isolation, and permanently deletes both synthetic accounts in ``finally``.

Required environment variables:
  API_BASE_URL
  TELEGRAM_BOT_TOKEN
  ADMIN_IDS
  QA_ADMIN_USER_JSON  - the admin's exact Telegram user object, re-signed here

Real broadcast delivery is disabled unless QA_ALLOW_BROADCAST=1 is explicitly
set. The default admin test only creates an in-memory preview token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests


KYIV_TZ = ZoneInfo("Europe/Kyiv")
SYNTHETIC_PREFIX = "999000"
ACCOUNT_DELETE_CONFIRMATION = "ВИДАЛИТИ"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

ROUTE_COVERAGE = {
    ("OPTIONS", "/{path_info:.*}"),
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


class QaError(RuntimeError):
    """Base class for predictable QA failures."""


class QaConfigError(QaError):
    pass


class QaHttpError(QaError):
    pass


class QaAssertionError(QaError):
    pass


class QaRunFailed(QaError):
    pass


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in TRUE_VALUES


def broadcast_confirmation_allowed(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return _env_flag(source, "QA_ALLOW_BROADCAST")


def make_init_data(
    user: Mapping[str, Any],
    token: str,
    *,
    auth_epoch: int | None = None,
) -> str:
    """Build Telegram WebApp initData without exposing the token."""
    if not token:
        raise QaConfigError("TELEGRAM_BOT_TOKEN is required")
    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise QaConfigError("Telegram user id must be a positive integer")
    params = {
        "auth_date": str(auth_epoch if auth_epoch is not None else int(time.time())),
        "query_id": f"qa-{secrets.token_hex(8)}",
        "user": json.dumps(dict(user), ensure_ascii=False, separators=(",", ":")),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(
        secret, data_check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def _fresh_synthetic_ids() -> tuple[int, int]:
    suffix = f"{int(time.time() * 1000) % 10_000_000:07d}{secrets.randbelow(90) + 10}"
    return int(f"{SYNTHETIC_PREFIX}{suffix}1"), int(f"{SYNTHETIC_PREFIX}{suffix}2")


def _parse_positive_float(raw: str | None, default: float, field: str) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise QaConfigError(f"{field} must be a number") from exc
    if value < 0:
        raise QaConfigError(f"{field} must not be negative")
    return value


def _validate_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise QaConfigError("API_BASE_URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QaConfigError("API_BASE_URL must not contain credentials/query/fragment")
    return value


def _synthetic_user(user_id: int, label: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "first_name": f"Ruby QA {label}",
        "username": f"ruby_qa_{str(user_id)[-8:].lower()}",
        "language_code": "uk",
    }


@dataclass(frozen=True)
class QaConfig:
    base_url: str
    token: str
    users: tuple[dict[str, Any], dict[str, Any]]
    admin_user: dict[str, Any]
    allow_broadcast: bool = False
    timeout_seconds: float = 30.0
    request_delay_seconds: float = 0.2
    admin_delay_seconds: float = 12.2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "QaConfig":
        source = os.environ if env is None else env
        base_url = _validate_base_url(str(source.get("API_BASE_URL", "")))
        token = str(source.get("TELEGRAM_BOT_TOKEN", "")).strip()
        if not token:
            raise QaConfigError("TELEGRAM_BOT_TOKEN is required")

        generated_a, generated_b = _fresh_synthetic_ids()
        try:
            user_a = int(source.get("QA_USER_ID_A", generated_a))
            user_b = int(source.get("QA_USER_ID_B", generated_b))
        except (TypeError, ValueError) as exc:
            raise QaConfigError("QA user ids must be integers") from exc
        if not str(user_a).startswith(SYNTHETIC_PREFIX) or not str(user_b).startswith(
            SYNTHETIC_PREFIX
        ):
            raise QaConfigError(f"QA user ids must start with {SYNTHETIC_PREFIX}")
        if user_a == user_b:
            raise QaConfigError("QA user ids must be distinct")

        raw_admin = str(source.get("QA_ADMIN_USER_JSON", "")).strip()
        if not raw_admin:
            raise QaConfigError(
                "QA_ADMIN_USER_JSON is required to preserve the real admin profile"
            )
        try:
            admin_user = json.loads(raw_admin)
        except json.JSONDecodeError as exc:
            raise QaConfigError("QA_ADMIN_USER_JSON must be valid JSON") from exc
        if not isinstance(admin_user, dict):
            raise QaConfigError("QA_ADMIN_USER_JSON must be a JSON object")
        admin_id = admin_user.get("id")
        if isinstance(admin_id, bool) or not isinstance(admin_id, int) or admin_id <= 0:
            raise QaConfigError("QA_ADMIN_USER_JSON.id must be a positive integer")
        admin_ids = {
            item.strip()
            for item in str(source.get("ADMIN_IDS", source.get("ADMIN_ID", ""))).split(",")
            if item.strip()
        }
        if str(admin_id) not in admin_ids:
            raise QaConfigError("QA admin id must be present in ADMIN_IDS")

        return cls(
            base_url=base_url,
            token=token,
            users=(_synthetic_user(user_a, "A"), _synthetic_user(user_b, "B")),
            admin_user=admin_user,
            allow_broadcast=broadcast_confirmation_allowed(source),
            timeout_seconds=_parse_positive_float(
                source.get("QA_TIMEOUT_SECONDS"), 30.0, "QA_TIMEOUT_SECONDS"
            ),
            request_delay_seconds=_parse_positive_float(
                source.get("QA_REQUEST_DELAY_SECONDS"),
                0.2,
                "QA_REQUEST_DELAY_SECONDS",
            ),
            admin_delay_seconds=_parse_positive_float(
                source.get("QA_ADMIN_DELAY_SECONDS"),
                12.2,
                "QA_ADMIN_DELAY_SECONDS",
            ),
        )


class ApiClient:
    """Small rate-aware client that never logs auth headers or response bodies."""

    def __init__(
        self,
        config: QaConfig,
        *,
        session: requests.Session | None = None,
        output: Callable[[str], None] = print,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.output = output
        self.sleeper = sleeper
        self._last_request = {"regular": 0.0, "admin": 0.0}

    def _throttle(self, path: str) -> None:
        scope = "admin" if path.startswith("/api/admin/") else "regular"
        delay = (
            self.config.admin_delay_seconds
            if scope == "admin"
            else self.config.request_delay_seconds
        )
        remaining = delay - (time.monotonic() - self._last_request[scope])
        if remaining > 0:
            self.sleeper(remaining)
        self._last_request[scope] = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        user: Mapping[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        _retry_count: int = 0,
    ) -> requests.Response:
        method = method.upper()
        if not path.startswith("/"):
            raise QaAssertionError("request path must be absolute")
        headers = {"Accept": "application/json"}
        if user is not None:
            headers["X-Telegram-Init-Data"] = make_init_data(user, self.config.token)
        self._throttle(path)
        try:
            response = self.session.request(
                method,
                f"{self.config.base_url}{path}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                params=dict(params) if params is not None else None,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise QaHttpError(
                f"{method} {path} transport failure ({type(exc).__name__})"
            ) from None

        self.output(f"    {method:<7} {path:<56} {response.status_code}")
        if response.status_code == 429:
            if _retry_count >= 3:
                raise QaHttpError(f"{method} {path}: rate limited after 3 retries")
            try:
                retry_after = min(65.0, max(0.0, float(response.headers.get("Retry-After", "1"))))
            except ValueError:
                retry_after = 1.0
            self.sleeper(retry_after + 0.1)
            return self.request(
                method,
                path,
                user=user,
                expected=expected,
                json_body=json_body,
                params=params,
                _retry_count=_retry_count + 1,
            )
        if response.status_code not in expected:
            detail = self._safe_detail(response)
            raise QaHttpError(
                f"{method} {path}: expected {expected}, got {response.status_code}{detail}"
            )
        return response

    @staticmethod
    def _safe_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        detail = payload.get("detail")
        code = payload.get("code")
        parts = []
        if isinstance(code, str):
            parts.append(f"code={code[:80]}")
        if isinstance(detail, str):
            parts.append(f"detail={detail[:160]}")
        return f" ({', '.join(parts)})" if parts else ""

    @staticmethod
    def json(response: requests.Response) -> Any:
        try:
            return response.json()
        except (ValueError, requests.JSONDecodeError):
            raise QaAssertionError("expected JSON response") from None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QaAssertionError(message)


class RegressionRunner:
    def __init__(
        self,
        config: QaConfig,
        *,
        client: ApiClient | None = None,
        output: Callable[[str], None] = print,
    ):
        self.config = config
        self.output = output
        self.client = client or ApiClient(config, output=output)
        self.user_a, self.user_b = config.users
        self.admin = config.admin_user
        self.user_ids = (int(self.user_a["id"]), int(self.user_b["id"]))
        self.now = datetime.now(KYIV_TZ)
        self.today = self.now.date()
        self.year = self.now.year
        self.month = self.now.month
        self.run_tag = secrets.token_hex(4)
        self.category = f"QA витрати {self.run_tag}"
        self.subcategory = f"QA підрозділ {self.run_tag}"
        self.employee = f"QA {self.run_tag}"
        self.time_category = f"QA фокус {self.run_tag}"
        self.default_expense = ""
        self.default_income = ""
        self.transaction_ids: list[int] = []
        self.primary_transaction_id: int | None = None
        self.track_id: int | None = None
        self.recurring_id: int | None = None
        self.results: list[tuple[str, bool, str]] = []

    def _case(self, name: str, operation: Callable[[], None]) -> None:
        self.output(f"\n=== {name} ===")
        try:
            operation()
        except QaError as exc:
            self.results.append((name, False, str(exc)))
            self.output(f"[FAIL] {name}: {exc}")
        except Exception as exc:
            self.results.append((name, False, type(exc).__name__))
            self.output(f"[FAIL] {name}: unexpected {type(exc).__name__}")
        else:
            self.results.append((name, True, ""))
            self.output(f"[PASS] {name}")

    def run_all(self) -> None:
        self.output("Ruby Finance production QA")
        self.output(f"API: {self.config.base_url}")
        self.output(f"Synthetic users: {self.user_ids[0]}, {self.user_ids[1]}")
        self.output(f"Kyiv date: {self.today.isoformat()}")
        self.output("Secrets/initData are intentionally not printed.")
        cases = (
            ("Public health, CORS and authentication", self._public_and_auth),
            ("Settings, categories and subcategories", self._categories),
            ("Transactions, idempotency and balance", self._transactions),
            ("Employees and generated categories", self._employees),
            ("Time categories and time tracking", self._time_tracking),
            ("Budgets and tenant isolation", self._budgets),
            ("Recurring operations and suggestions", self._recurring),
            ("All financial reports", self._reports),
            ("Tax, notifications and settings isolation", self._settings),
            ("Owner-scoped CRUD cleanup routes", self._resource_deletes),
            ("Admin read routes and broadcast safety gate", self._admin_routes),
        )
        for name, operation in cases:
            self._case(name, operation)
        passed = sum(result[1] for result in self.results)
        failed = len(self.results) - passed
        self.output(f"\nQA cases: {passed} passed, {failed} failed")
        if failed:
            failed_names = ", ".join(name for name, ok, _ in self.results if not ok)
            raise QaRunFailed(f"failed groups: {failed_names}")

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.client.json(self.client.request(method, path, **kwargs))

    def _public_and_auth(self) -> None:
        health = self._request_json("GET", "/api/health", expected=(200,))
        _require(health.get("ok") is True, "health must report ok=true")
        rates = self._request_json("GET", "/api/exchange-rates")
        _require(all(rates.get(code) for code in ("USD", "EUR")), "rates are incomplete")
        self.client.request("OPTIONS", "/api/me", expected=(204,))
        self.client.request("GET", "/api/me", expected=(401,))
        a = self._request_json("GET", "/api/me", user=self.user_a)
        b = self._request_json("GET", "/api/me", user=self.user_b)
        _require(str(a.get("id")) == str(self.user_a["id"]), "user A identity mismatch")
        _require(str(b.get("id")) == str(self.user_b["id"]), "user B identity mismatch")

    def _categories(self) -> None:
        categories_a = self._request_json("GET", "/api/categories", user=self.user_a)
        categories_b = self._request_json("GET", "/api/categories", user=self.user_b)
        settings_a = self._request_json("GET", "/api/settings", user=self.user_a)
        settings_b = self._request_json("GET", "/api/settings", user=self.user_b)
        _require(settings_a.get("tax_year") == self.year, "current tax year mismatch")
        _require(settings_b.get("tax_year") == self.year, "user B tax year mismatch")
        self.default_expense = next(iter(categories_a.get("expense") or ()), "")
        self.default_income = next(iter(categories_a.get("income") or ()), "")
        _require(bool(self.default_expense and self.default_income), "default categories missing")
        _require(categories_a == categories_b, "fresh users must start with equal defaults")

        created = self._request_json(
            "POST",
            "/api/categories",
            user=self.user_a,
            expected=(201,),
            json_body={
                "type": "expense",
                "name": self.category,
                "emoji": "🧪",
                "keywords": ["qa"],
            },
        )
        _require(created.get("name") == self.category, "category create mismatch")
        encoded_category = quote(self.category, safe="")
        self._request_json(
            "POST",
            f"/api/categories/expense/{encoded_category}/subcategories",
            user=self.user_a,
            expected=(201,),
            json_body={"name": self.subcategory},
        )
        patched = self._request_json(
            "PATCH",
            f"/api/categories/expense/{encoded_category}",
            user=self.user_a,
            json_body={"emoji": "📊", "keywords": ["qa", "regression"]},
        )
        _require(patched.get("subcategories") == [self.subcategory], "subcategory lost on patch")
        full_a = self._request_json("GET", "/api/categories/full", user=self.user_a)
        full_b = self._request_json("GET", "/api/categories/full", user=self.user_b)
        _require(self.category in full_a.get("expense", {}), "user A category missing")
        _require(self.category not in full_b.get("expense", {}), "category leaked to user B")

    def _create_transaction(
        self,
        user: Mapping[str, Any],
        *,
        amount: float,
        kind: str,
        category: str,
        request_id: str,
        subcategory: str | None = None,
        source: str | None = None,
        description: str = "QA regression",
    ) -> dict[str, Any]:
        body = {
            "amount": amount,
            "currency": "UAH",
            "type": kind,
            "category": category,
            "description": description,
            "client_request_id": request_id,
            "payment_source": source,
        }
        if subcategory is not None:
            body["subcategory"] = subcategory
        created = self._request_json(
            "POST", "/api/transactions", user=user, expected=(201,), json_body=body
        )
        _require(created.get("duplicate") is False, "new transaction marked duplicate")
        return created

    def _transactions(self) -> None:
        primary_body = {
            "amount": 125.50,
            "currency": "UAH",
            "type": "expense",
            "category": self.category,
            "subcategory": self.subcategory,
            "description": "QA primary expense",
            "client_request_id": f"qa-{self.run_tag}-expense",
            "payment_source": "cash",
        }
        primary = self._request_json(
            "POST",
            "/api/transactions",
            user=self.user_a,
            expected=(201,),
            json_body=primary_body,
        )
        replay = self._request_json(
            "POST", "/api/transactions", user=self.user_a, json_body=primary_body
        )
        _require(replay.get("id") == primary.get("id"), "idempotent replay changed id")
        _require(replay.get("duplicate") is True, "idempotent replay not marked duplicate")
        self.primary_transaction_id = int(primary["id"])
        self.transaction_ids.append(self.primary_transaction_id)

        income = self._create_transaction(
            self.user_a,
            amount=600,
            kind="income",
            category=self.default_income,
            request_id=f"qa-{self.run_tag}-income",
            source="transfer",
        )
        foreign = self._create_transaction(
            self.user_b,
            amount=77,
            kind="expense",
            category=self.default_expense,
            request_id=f"qa-{self.run_tag}-foreign",
            source="card",
        )
        self.transaction_ids.append(int(income["id"]))
        patched = self._request_json(
            "PATCH",
            f"/api/transactions/{self.primary_transaction_id}",
            user=self.user_a,
            json_body={"payment_source": "card"},
        )
        _require(patched.get("payment_source") == "card", "transaction patch failed")
        self.client.request(
            "DELETE",
            f"/api/transactions/{self.primary_transaction_id}",
            user=self.user_b,
            expected=(404,),
        )

        rows_a = self._request_json(
            "GET",
            "/api/transactions",
            user=self.user_a,
            params={"period": "current_month", "limit": 5000},
        )
        rows_b = self._request_json("GET", "/api/transactions", user=self.user_b)
        a_ids = {row.get("id") for row in rows_a}
        b_ids = {row.get("id") for row in rows_b}
        _require(self.primary_transaction_id in a_ids, "user A transaction missing")
        _require(self.primary_transaction_id not in b_ids, "transaction leaked to user B")
        _require(int(foreign["id"]) in b_ids, "user B fixture missing")
        expenses = self._request_json(
            "GET", "/api/transactions", user=self.user_a, params={"type": "expense"}
        )
        _require(all(row.get("type") == "expense" for row in expenses), "type filter failed")
        quick = self._request_json("GET", "/api/quick-templates", user=self.user_a)
        _require(isinstance(quick.get("templates"), list), "quick templates malformed")
        balance = self._request_json(
            "GET",
            "/api/balance",
            user=self.user_a,
            params={"year": self.year, "month": self.month},
        )
        _require(balance.get("income", 0) >= 600, "balance omitted income")

    def _employees(self) -> None:
        self._request_json(
            "POST",
            "/api/employees",
            user=self.user_a,
            expected=(201,),
            json_body={"name": self.employee},
        )
        employees_a = self._request_json("GET", "/api/employees", user=self.user_a)
        employees_b = self._request_json("GET", "/api/employees", user=self.user_b)
        _require(self.employee in employees_a, "employee missing for owner")
        _require(self.employee not in employees_b, "employee leaked to user B")
        categories = self._request_json("GET", "/api/categories/full", user=self.user_a)
        _require(f"Від {self.employee}" in categories.get("income", {}), "income employee category missing")
        _require(f"ЗП {self.employee}" in categories.get("expense", {}), "salary employee category missing")
        for kind, category, amount, suffix in (
            ("income", f"Від {self.employee}", 300, "emp-income"),
            ("expense", f"ЗП {self.employee}", 100, "emp-salary"),
        ):
            row = self._create_transaction(
                self.user_a,
                amount=amount,
                kind=kind,
                category=category,
                request_id=f"qa-{self.run_tag}-{suffix}",
                source="transfer",
            )
            self.transaction_ids.append(int(row["id"]))
        self.client.request(
            "DELETE",
            f"/api/employees/{quote(self.employee, safe='')}",
            user=self.user_b,
            expected=(404,),
        )

    def _time_tracking(self) -> None:
        self._request_json(
            "POST",
            "/api/time-categories",
            user=self.user_a,
            expected=(201,),
            json_body={"name": self.time_category, "emoji": "⏱"},
        )
        categories_a = self._request_json("GET", "/api/time-categories", user=self.user_a)
        categories_b = self._request_json("GET", "/api/time-categories", user=self.user_b)
        _require(self.time_category in categories_a, "time category missing")
        _require(self.time_category not in categories_b, "time category leaked")
        body = {
            "minutes": 90,
            "category": self.time_category,
            "description": "QA focused work",
            "client_request_id": f"qa-{self.run_tag}-time",
        }
        track = self._request_json(
            "POST", "/api/time-tracks", user=self.user_a, expected=(201,), json_body=body
        )
        replay = self._request_json(
            "POST", "/api/time-tracks", user=self.user_a, json_body=body
        )
        _require(replay.get("id") == track.get("id"), "time replay changed id")
        _require(replay.get("duplicate") is True, "time replay not marked duplicate")
        self.track_id = int(track["id"])
        tracks_a = self._request_json(
            "GET",
            "/api/time-tracks",
            user=self.user_a,
            params={"year": self.year, "month": self.month, "limit": 500},
        )
        tracks_b = self._request_json("GET", "/api/time-tracks", user=self.user_b)
        _require(any(row.get("id") == self.track_id for row in tracks_a), "time track missing")
        _require(not any(row.get("id") == self.track_id for row in tracks_b), "time track leaked")
        self.client.request(
            "DELETE",
            f"/api/time-tracks/{self.track_id}",
            user=self.user_b,
            expected=(404,),
        )
        self.client.request(
            "DELETE",
            f"/api/time-categories/{quote(self.time_category, safe='')}",
            user=self.user_b,
            expected=(404,),
        )

    def _budgets(self) -> None:
        budget = self._request_json(
            "PUT",
            "/api/budgets",
            user=self.user_a,
            json_body={
                "type": "expense",
                "category": self.category,
                "monthly_limit_uah": 1000,
            },
        )
        _require(budget.get("category") == self.category, "budget create mismatch")
        a = self._request_json(
            "GET",
            "/api/budgets",
            user=self.user_a,
            params={"year": self.year, "month": self.month},
        )
        b = self._request_json(
            "GET",
            "/api/budgets",
            user=self.user_b,
            params={"year": self.year, "month": self.month},
        )
        _require(any(row.get("category") == self.category for row in a.get("budgets", [])), "budget missing")
        _require(not any(row.get("category") == self.category for row in b.get("budgets", [])), "budget leaked")
        self.client.request(
            "DELETE",
            f"/api/budgets/expense/{quote(self.category, safe='')}",
            user=self.user_b,
            expected=(404,),
        )

    def _recurring(self) -> None:
        start = (self.today + timedelta(days=1)).isoformat()
        recurring = self._request_json(
            "POST",
            "/api/recurring-operations",
            user=self.user_a,
            expected=(201,),
            json_body={
                "type": "expense",
                "amount": 49.90,
                "currency": "UAH",
                "category": self.category,
                "subcategory": self.subcategory,
                "description": "QA monthly plan",
                "payment_source": "card",
                "frequency": "monthly",
                "interval": 1,
                "start_date": start,
                "auto_create": False,
            },
        )
        self.recurring_id = int(recurring["id"])
        rows_a = self._request_json("GET", "/api/recurring-operations", user=self.user_a)
        rows_b = self._request_json("GET", "/api/recurring-operations", user=self.user_b)
        _require(any(row.get("id") == self.recurring_id for row in rows_a), "recurring template missing")
        _require(not any(row.get("id") == self.recurring_id for row in rows_b), "recurring template leaked")
        paused = self._request_json(
            "PATCH",
            f"/api/recurring-operations/{self.recurring_id}",
            user=self.user_a,
            json_body={"active": False},
        )
        _require(paused.get("active") is False, "recurring pause failed")
        resumed = self._request_json(
            "PATCH",
            f"/api/recurring-operations/{self.recurring_id}",
            user=self.user_a,
            json_body={"active": True},
        )
        _require(resumed.get("active") is True, "recurring resume failed")
        suggestions = self._request_json("GET", "/api/recurring-suggestions", user=self.user_a)
        _require(isinstance(suggestions, list), "recurring suggestions malformed")
        self.client.request(
            "DELETE",
            f"/api/recurring-operations/{self.recurring_id}",
            user=self.user_b,
            expected=(404,),
        )

    def _reports(self) -> None:
        params = {"year": self.year, "month": self.month}
        balance = self._request_json("GET", "/api/balance", user=self.user_a, params=params)
        monthly = self._request_json("GET", "/api/reports/monthly", user=self.user_a, params=params)
        _require(monthly.get("total_income") == balance.get("income"), "monthly income differs from balance")
        _require(monthly.get("total_expense") == balance.get("expense"), "monthly expense differs from balance")
        sources = self._request_json("GET", "/api/reports/payment-sources", user=self.user_a, params=params)
        _require("expense_by_payment_source" in sources, "payment source report malformed")
        employees = self._request_json("GET", "/api/reports/employees", user=self.user_a, params=params)
        _require(any(row.get("name") == self.employee for row in employees), "employee report omitted QA employee")
        tax = self._request_json("GET", "/api/reports/tax", user=self.user_a, params=params)
        _require(tax.get("rules_year") == self.year, "tax report year mismatch")
        accounting = self._request_json("GET", "/api/reports/accounting", user=self.user_a, params=params)
        _require(isinstance(accounting.get("entries"), list), "accounting report malformed")
        time_report = self._request_json("GET", "/api/reports/time", user=self.user_a, params=params)
        _require(time_report.get("total_minutes", 0) >= 90, "time report omitted QA track")
        breakdown = self._request_json(
            "GET",
            "/api/reports/category-breakdown",
            user=self.user_a,
            params={
                **params,
                "period": "month",
                "type": "expense",
                "category": self.category,
            },
        )
        _require(breakdown.get("total", 0) >= 125.5, "subcategory breakdown omitted transaction")
        insights = self._request_json("GET", "/api/insights", user=self.user_a)
        _require(isinstance(insights, list), "insights response malformed")
        monday = self.today - timedelta(days=self.today.weekday())
        digest = self._request_json(
            "GET",
            "/api/digest/weekly",
            user=self.user_a,
            params={"week_start": monday.isoformat()},
        )
        _require(digest.get("period_start") == monday.isoformat(), "digest period mismatch")
        forecast = self._request_json(
            "GET", "/api/forecast", user=self.user_a, params=params
        )
        _require(forecast.get("basis") == "recorded_plus_scheduled", "forecast basis mismatch")

    def _settings(self) -> None:
        notifications = self._request_json(
            "GET", "/api/settings/notifications", user=self.user_a
        )
        _require(notifications.get("weekly_digest_enabled") is False, "digest must default off")
        enabled = self._request_json(
            "PATCH",
            "/api/settings/notifications",
            user=self.user_a,
            json_body={"weekly_digest_enabled": True},
        )
        _require(enabled.get("weekly_digest_enabled") is True, "digest opt-in failed")
        self._request_json(
            "PATCH",
            "/api/settings/notifications",
            user=self.user_a,
            json_body={"weekly_digest_enabled": False},
        )
        changed = self._request_json(
            "PATCH",
            "/api/settings/tax",
            user=self.user_a,
            json_body={"year": self.year, "group": "none"},
        )
        _require(changed.get("group") == "none", "tax group update failed")
        a = self._request_json("GET", "/api/settings", user=self.user_a)
        b = self._request_json("GET", "/api/settings", user=self.user_b)
        _require(a.get("tax_profile", {}).get("group") == "none", "user A tax group not saved")
        _require(b.get("tax_profile", {}).get("group") != "none", "tax settings leaked to user B")

    def _resource_deletes(self) -> None:
        if self.recurring_id is not None:
            self.client.request(
                "DELETE",
                f"/api/recurring-operations/{self.recurring_id}",
                user=self.user_a,
                expected=(204,),
            )
        self.client.request(
            "DELETE",
            f"/api/budgets/expense/{quote(self.category, safe='')}",
            user=self.user_a,
            expected=(204,),
        )
        if self.track_id is not None:
            self.client.request(
                "DELETE", f"/api/time-tracks/{self.track_id}", user=self.user_a, expected=(204,)
            )
        self.client.request(
            "DELETE",
            f"/api/time-categories/{quote(self.time_category, safe='')}",
            user=self.user_a,
            expected=(204,),
        )
        self.client.request(
            "DELETE",
            f"/api/employees/{quote(self.employee, safe='')}",
            user=self.user_a,
            expected=(204,),
        )
        encoded_category = quote(self.category, safe="")
        self.client.request(
            "DELETE",
            f"/api/categories/expense/{encoded_category}/subcategories/{quote(self.subcategory, safe='')}",
            user=self.user_b,
            expected=(404,),
        )
        self.client.request(
            "DELETE",
            f"/api/categories/expense/{encoded_category}/subcategories/{quote(self.subcategory, safe='')}",
            user=self.user_a,
            expected=(204,),
        )
        self.client.request(
            "DELETE",
            f"/api/categories/expense/{encoded_category}",
            user=self.user_b,
            expected=(404,),
        )
        self.client.request(
            "DELETE",
            f"/api/categories/expense/{encoded_category}",
            user=self.user_a,
            expected=(204,),
        )
        if self.primary_transaction_id is not None:
            self.client.request(
                "DELETE",
                f"/api/transactions/{self.primary_transaction_id}",
                user=self.user_a,
                expected=(204,),
            )
        reset = self._request_json("DELETE", "/api/settings", user=self.user_a)
        _require(isinstance(reset, dict), "settings reset response malformed")
        restored = self._request_json("GET", "/api/settings", user=self.user_a)
        _require(restored.get("tax_profile", {}).get("group") == "fop3", "settings reset failed")

    def _admin_routes(self) -> None:
        text = f"Ruby Finance QA preview {self.run_tag}"
        preview = self._request_json(
            "POST",
            "/api/admin/broadcast",
            user=self.admin,
            json_body={"text": text},
        )
        _require(preview.get("preview") is True, "broadcast preview missing")
        confirmation_token = preview.get("confirmation_token")
        _require(isinstance(confirmation_token, str) and confirmation_token, "preview token missing")
        broadcast_id = None
        if self.config.allow_broadcast:
            self.output("    WARNING: QA_ALLOW_BROADCAST=1; confirming delivery to real users.")
            confirmed = self._request_json(
                "POST",
                "/api/admin/broadcast",
                user=self.admin,
                json_body={
                    "text": text,
                    "confirm": True,
                    "confirmation_token": confirmation_token,
                },
            )
            _require(confirmed.get("confirmed") is True, "broadcast confirmation failed")
            broadcast_id = confirmed.get("broadcast_id")
        else:
            self.output("    Broadcast confirmation skipped (QA_ALLOW_BROADCAST is not 1).")

        users = self._request_json("GET", "/api/admin/users", user=self.admin)
        ids = {str(row.get("user_id")) for row in users.get("users", [])}
        _require(all(str(user_id) in ids for user_id in self.user_ids), "QA users absent from admin roster")
        broadcasts = self._request_json("GET", "/api/admin/broadcasts", user=self.admin)
        _require(isinstance(broadcasts.get("broadcasts"), list), "broadcast history malformed")
        audit = self._request_json("GET", "/api/admin/audit", user=self.admin)
        _require(isinstance(audit.get("events"), list), "admin audit malformed")
        if broadcast_id is None and broadcasts["broadcasts"]:
            broadcast_id = broadcasts["broadcasts"][0].get("id")
        if broadcast_id is None:
            self.client.request(
                "GET", "/api/admin/broadcasts/2147483647", user=self.admin, expected=(404,)
            )
        else:
            detail = self._request_json(
                "GET", f"/api/admin/broadcasts/{int(broadcast_id)}", user=self.admin
            )
            _require("broadcast" in detail and "receipts" in detail, "broadcast detail malformed")

    def delete_account(self, user_id: int) -> None:
        user = next(user for user in self.config.users if int(user["id"]) == int(user_id))
        response = self.client.request(
            "DELETE",
            "/api/account",
            user=user,
            expected=(200, 404, 410),
            json_body={"confirmation": ACCOUNT_DELETE_CONFIRMATION},
        )
        self.output(f"    account cleanup {user_id}: {response.status_code}")


def run_with_cleanup(runner: Any) -> None:
    primary_error: BaseException | None = None
    try:
        runner.run_all()
    except BaseException as exc:
        primary_error = exc
    cleanup_errors: list[str] = []
    for user_id in runner.user_ids:
        try:
            runner.delete_account(user_id)
        except Exception as exc:
            cleanup_errors.append(f"{user_id}:{type(exc).__name__}")
    if primary_error is not None:
        if cleanup_errors:
            print(f"Cleanup also failed for: {', '.join(cleanup_errors)}")
        raise primary_error
    if cleanup_errors:
        raise QaRunFailed(f"account cleanup failed for: {', '.join(cleanup_errors)}")


def main() -> int:
    _configure_utf8_console()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    try:
        config = QaConfig.from_env()
        run_with_cleanup(RegressionRunner(config))
    except QaError as exc:
        print(f"QA FAILED: {exc}")
        return 1
    except KeyboardInterrupt:
        print("QA interrupted")
        return 130
    print("QA PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
