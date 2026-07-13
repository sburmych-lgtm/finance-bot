"""Release-hardening contracts that intentionally precede implementation.

These tests cover security, privacy, and reporting edge cases found during the
release review.  They describe the intended behavior without coupling to a
particular implementation strategy.
"""

import asyncio
import copy
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from types import SimpleNamespace
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
import pytest

import bot


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body)


def use_database(monkeypatch, tmp_path, name="release-hardening.db"):
    database = bot.Database(str(tmp_path / name))
    monkeypatch.setattr(bot, "db", database)
    return database


class Request(dict):
    def __init__(self, user_id="user-1", query=None, body=None, match_info=None):
        super().__init__(user_id=str(user_id), tg_user={"id": user_id})
        self.rel_url = SimpleNamespace(query=query or {})
        self._body = body
        self.match_info = match_info or {}

    async def json(self):
        return self._body


def signed_init_data(token, user_id):
    params = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})


def admin_update(admin_id="42"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_id),
        message=FakeMessage(),
    )


def allow_all_limiter():
    return SimpleNamespace(
        check=lambda _key: SimpleNamespace(allowed=True, retry_after=0)
    )


class FakeRateResponse:
    status = 200

    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.rows


class FakeRateSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return FakeRateResponse(self.rows)


def test_exchange_rate_staleness_uses_total_elapsed_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bot,
        "exchange_rates_cache",
        {
            "USD": 41.5,
            "EUR": 45.2,
            "last_update": datetime.now(bot.KYIV_TZ) - timedelta(days=2, minutes=5),
        },
    )

    async def refresh():
        calls.append("refresh")

    monkeypatch.setattr(bot, "update_exchange_rates", refresh)

    assert run(bot.get_exchange_rate("USD")) == 41.5
    assert calls == ["refresh"]


def test_incomplete_rate_refresh_keeps_the_complete_last_good_snapshot(monkeypatch):
    last_update = datetime(2026, 7, 12, 10, 0, tzinfo=bot.KYIV_TZ)
    last_good = {"USD": 41.5, "EUR": 45.2, "last_update": last_update}
    monkeypatch.setattr(bot, "exchange_rates_cache", dict(last_good))
    monkeypatch.setattr(
        bot.aiohttp,
        "ClientSession",
        lambda *_args, **_kwargs: FakeRateSession([{"cc": "USD", "rate": 50.0}]),
    )

    run(bot.update_exchange_rates())

    assert bot.exchange_rates_cache == last_good


def test_cold_rate_failure_is_explicit_for_api_and_bot_flow(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot,
        "exchange_rates_cache",
        {"USD": None, "EUR": None, "last_update": None},
    )

    async def failed_refresh():
        return False

    monkeypatch.setattr(bot, "update_exchange_rates", failed_refresh)

    response = run(bot.api_exchange_rates(Request()))
    assert response.status == 503
    assert payload(response)["code"] == "EXCHANGE_RATE_UNAVAILABLE"

    query = SimpleNamespace(
        answers=[],
        answer=lambda *_args, **_kwargs: None,
    )

    async def answer(text, **kwargs):
        query.answers.append((text, kwargs))

    query.answer = answer
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id="user-1"),
    )
    run(
        bot.save_transaction(
            update, SimpleNamespace(), "expense", "Інше", "USD", "10"
        )
    )
    assert query.answers
    assert query.answers[-1][1].get("show_alert") is True


def test_time_track_api_rejects_fractional_numeric_minutes(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)

    response = run(
        bot.api_time_tracks_create(
            Request(body={"minutes": 1.5, "category": "Інше", "description": ""})
        )
    )

    assert response.status == 400


def test_time_report_never_returns_negative_untracked_minutes(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def exercise():
        await database.add_time_track(
            "user-1",
            50_000,
            "Робота",
            "imported aggregate",
            "2026-02-10",
            "2026-02-10 12:00:00",
        )
        return payload(
            await bot.api_report_time(
                Request(query={"year": "2026", "month": "2"})
            )
        )

    result = run(exercise())

    assert result["total_minutes"] == 50_000
    assert result["untracked_minutes"] == 0


def test_cleanup_removes_only_synthetic_users_and_all_their_dependencies(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", allow_all_limiter())
    connection = database.conn
    connection.executemany(
        "INSERT INTO users (user_id, first_name) VALUES (?, ?)",
        (("1001", "Real inactive user"), ("9990001", "Synthetic QA user")),
    )
    connection.executemany(
        "INSERT INTO user_settings (user_id, settings_json) VALUES (?, '{}')",
        (("1001",), ("9990001",)),
    )
    connection.execute(
        """INSERT INTO budgets
           (user_id, type, category, monthly_limit_uah)
           VALUES ('9990001', 'expense', 'Інше', 1000)"""
    )
    connection.execute(
        """INSERT INTO recurring_operations
           (user_id, type, amount, currency, amount_uah, category,
            description, frequency, interval, start_date, anchor_day,
            next_due_date)
           VALUES ('9990001', 'expense', 100, 'UAH', 100, 'Інше',
                   'synthetic', 'monthly', 1, '2026-07-01', 1, '2026-08-01')"""
    )
    connection.execute(
        """INSERT INTO notification_preferences
           (user_id, weekly_digest_enabled) VALUES ('9990001', 1)"""
    )
    connection.execute(
        """INSERT INTO notification_deliveries
           (user_id, kind, period_key, status)
           VALUES ('9990001', 'weekly_digest', '2026-W28', 'sent')"""
    )
    connection.execute(
        "INSERT INTO subscriptions (user_id, plan) VALUES ('9990001', 'vip')"
    )
    broadcast_id = connection.execute(
        """INSERT INTO broadcasts (text, created_at)
           VALUES ('QA', '2026-07-12 10:00:00')"""
    ).lastrowid
    connection.execute(
        """INSERT INTO broadcast_receipts
           (broadcast_id, user_id, status, created_at)
           VALUES (?, '9990001', 'sent', '2026-07-12 10:01:00')""",
        (broadcast_id,),
    )
    connection.commit()

    run(bot.admin_cleanup_users(admin_update(), SimpleNamespace()))

    assert connection.execute(
        "SELECT 1 FROM users WHERE user_id = '1001'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT 1 FROM user_settings WHERE user_id = '1001'"
    ).fetchone() is not None
    for table in (
        "users",
        "user_settings",
        "subscriptions",
        "budgets",
        "recurring_operations",
        "notification_preferences",
        "notification_deliveries",
        "broadcast_receipts",
    ):
        assert connection.execute(
            f"SELECT 1 FROM {table} WHERE user_id = '9990001'"
        ).fetchone() is None, table


def test_sentry_scrubs_breadcrumb_and_logentry_payloads():
    init_data = "signed-init-data-must-not-leave-the-process"
    financial_note = "private invoice 2026-07 amount 42000"
    event = {
        "breadcrumbs": {
            "values": [
                {
                    "category": "request",
                    "message": "request accepted",
                    "data": {
                        "X-Telegram-Init-Data": init_data,
                        "payload": {"description": financial_note},
                    },
                }
            ]
        },
        "logentry": {
            "message": "transaction payload %s",
            "formatted": f"transaction payload {financial_note}",
            "params": [financial_note],
        },
    }

    sanitized = bot.sanitize_sentry_event(event, hint={})
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert init_data not in serialized
    assert financial_note not in serialized
    assert "request accepted" in serialized


def test_employee_report_uses_null_roi_when_salary_is_zero(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def exercise():
        await database.add_transaction(
            "user-1",
            1500,
            "UAH",
            1500,
            "income",
            "Від Alice",
            "client income",
            "2026-07-10",
            "2026-07-10 10:00:00",
        )
        return payload(
            await bot.api_report_employees(
                Request(query={"year": "2026", "month": "7"})
            )
        )

    result = run(exercise())

    assert result == [
        {
            "name": "Alice",
            "income": 1500.0,
            "salary": 0,
            "profit": 1500.0,
            "roi": None,
        }
    ]


def test_ai_report_displays_dash_for_undefined_roi(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    settings = copy.deepcopy(bot.DEFAULT_SETTINGS)
    settings["employees"] = ["Alice"]
    now = datetime.now(bot.KYIV_TZ)
    captured = []

    async def edit_message_text(text, **_kwargs):
        captured.append(text)

    async def exercise():
        await database.save_user_settings("user-1", settings)
        await database.add_transaction(
            "user-1", 100, "UAH", 100, "income", "Від Alice", "income",
            now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await bot.show_ai_report(
            SimpleNamespace(
                effective_user=SimpleNamespace(id="user-1"),
                callback_query=SimpleNamespace(edit_message_text=edit_message_text),
            ),
            SimpleNamespace(),
        )

    run(exercise())
    assert "ROI: —" in captured[0]


def test_accounting_report_includes_payment_source_breakdown(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def exercise():
        rows = (
            (100, "income", "cash"),
            (250, "income", "card"),
            (40, "expense", "transfer"),
            (10, "expense", None),
            (15, "expense", "other"),
        )
        for index, (amount, kind, source) in enumerate(rows):
            await database.add_transaction(
                "user-1",
                amount,
                "UAH",
                amount,
                kind,
                "Інше",
                f"row-{index}",
                "2026-07-10",
                f"2026-07-10 10:00:0{index}",
                payment_source=source,
            )
        return payload(
            await bot.api_report_accounting(
                Request(query={"year": "2026", "month": "7"})
            )
        )

    result = run(exercise())

    assert result["income_by_payment_source"]["cash"] == 100.0
    assert result["income_by_payment_source"]["card"] == 250.0
    assert result["expense_by_payment_source"]["transfer"] == 40.0
    assert result["expense_by_payment_source"]["unclassified"] == 10.0
    assert result["expense_by_payment_source"]["other"] == 15.0
    assert result["model"] == "simplified_cash_movement"
    assert result["disclaimer"]
    entries = {
        (entry["type"], entry["payment_source"]): entry
        for entry in result["entries"]
    }
    assert entries[("income", "cash")]["debit"] == "301"
    assert entries[("income", "card")]["debit"] == "311"
    assert entries[("expense", "transfer")]["credit"] == "311"
    assert entries[("expense", "other")]["source_class"] == "other"
    assert entries[("expense", "other")]["credit"] == "—"
    assert entries[("expense", "unclassified")]["source_class"] == "unclassified"
    assert entries[("expense", "unclassified")]["credit"] == "—"


def test_user_facing_bot_handle_comes_from_environment(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    expected = "@ruby_release_bot"
    monkeypatch.setenv("TELEGRAM_BOT_HANDLE", expected)
    monkeypatch.setenv("BOT_HANDLE", "@fallback_should_not_win")
    user = SimpleNamespace(
        id=77,
        username="release-user",
        first_name="Release",
        last_name="User",
        language_code="uk",
    )
    start_message = FakeMessage()
    info_message = FakeMessage()

    async def exercise():
        await bot.start(
            SimpleNamespace(effective_user=user, message=start_message),
            SimpleNamespace(),
        )
        await bot.show_info(
            SimpleNamespace(effective_user=user, message=info_message),
            SimpleNamespace(),
        )

    run(exercise())

    # The Mini App-first welcome intentionally shows no bot handle (just the
    # app-open button); it must still never leak the legacy handle.
    assert "застосунок" in start_message.replies[0]["text"]
    assert expected in info_message.replies[0]["text"]
    assert "@Olesia_money_bot" not in start_message.replies[0]["text"]
    assert "@Olesia_money_bot" not in info_message.replies[0]["text"]
    assert run(database.get_all_user_ids()) == ["77"]


def test_cors_uses_deployment_origin_allowlist_and_omits_header_for_denied_origin(
    monkeypatch
):
    allowed_origin = "https://mini.example"
    monkeypatch.setenv("MINIAPP_PUBLIC_URL", allowed_origin)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            allowed = await client.options(
                "/api/me", headers={"Origin": allowed_origin}
            )
            denied = await client.options(
                "/api/me", headers={"Origin": "https://attacker.example"}
            )
            return (
                allowed.status,
                allowed.headers.get("Access-Control-Allow-Origin"),
                denied.status,
                denied.headers.get("Access-Control-Allow-Origin"),
            )

    allowed_status, allowed_header, denied_status, denied_header = run(exercise())

    assert allowed_status == denied_status == 204
    assert allowed_header == allowed_origin
    assert denied_header is None


def test_list_handlers_never_pass_an_unbounded_limit_to_database(monkeypatch):
    calls = {}

    class RecordingDatabase:
        async def get_transactions(self, _user_id, **kwargs):
            calls["transactions"] = kwargs.get("limit")
            return []

        async def get_time_tracks(
            self, _user_id, year=None, month=None, limit=None
        ):
            calls["time_tracks"] = limit
            return []

    monkeypatch.setattr(bot, "db", RecordingDatabase())

    transaction_response = run(
        bot.api_get_transactions(Request(query={"limit": "all"}))
    )
    time_response = run(bot.api_time_tracks_list(Request()))

    if transaction_response.status == 200:
        assert isinstance(calls.get("transactions"), int)
        assert 1 <= calls["transactions"] <= 5000
    else:
        assert transaction_response.status == 400
    assert time_response.status == 200
    assert isinstance(calls.get("time_tracks"), int)
    assert 1 <= calls["time_tracks"] <= 500


def test_concurrent_inflight_write_finishes_before_delete_and_cannot_leave_orphans(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    token = "deletion-race-token"
    user_id = "deleted-user"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    headers = {"X-Telegram-Init-Data": signed_init_data(token, user_id)}
    entered_write = asyncio.Event()
    release_write = asyncio.Event()
    delete_entered = asyncio.Event()
    original_add = database.add_transaction
    original_delete = database.delete_user_account

    async def delayed_add(*args, **kwargs):
        entered_write.set()
        await release_write.wait()
        return await original_add(*args, **kwargs)

    monkeypatch.setattr(database, "add_transaction", delayed_add)

    async def observed_delete(*args, **kwargs):
        delete_entered.set()
        return await original_delete(*args, **kwargs)

    monkeypatch.setattr(database, "delete_user_account", observed_delete)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            write_task = asyncio.create_task(client.post(
                "/api/transactions",
                headers=headers,
                json={"type": "expense", "amount": 10, "category": "Інше"},
            ))
            await entered_write.wait()
            delete_task = asyncio.create_task(client.delete(
                "/api/account", headers=headers,
                json={"confirmation": bot.ACCOUNT_DELETE_CONFIRMATION},
            ))
            try:
                await asyncio.wait_for(delete_entered.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
            release_write.set()
            written, deleted = await asyncio.gather(write_task, delete_task)
            counts_after_delete = {
                table: database.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                for table in ("users", "transactions", "user_settings")
            }
            fresh = await client.get("/api/me", headers=headers)
            fresh_user_count = database.conn.execute(
                "SELECT COUNT(*) FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            fresh_transaction_count = database.conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            return (
                written.status,
                deleted.status,
                counts_after_delete,
                fresh.status,
                fresh_user_count,
                fresh_transaction_count,
            )

    written, deleted, after_delete, fresh, fresh_users, fresh_transactions = run(
        exercise()
    )
    assert written == 201
    assert deleted == 200
    assert after_delete == {"users": 0, "transactions": 0, "user_settings": 0}
    assert fresh == 200
    assert fresh_users == 1
    assert fresh_transactions == 0


def test_bot_account_delete_waits_for_inflight_miniapp_write(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    token = "bot-deletion-race-token"
    user_id = "bot-deleted-user"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    headers = {"X-Telegram-Init-Data": signed_init_data(token, user_id)}
    entered_write = asyncio.Event()
    release_write = asyncio.Event()
    original_add = database.add_transaction

    async def delayed_add(*args, **kwargs):
        entered_write.set()
        await release_write.wait()
        return await original_add(*args, **kwargs)

    monkeypatch.setattr(database, "add_transaction", delayed_add)

    class DeleteQuery:
        data = "account_delete:confirm"
        from_user = SimpleNamespace(id=user_id)

        async def answer(self):
            return None

        async def edit_message_text(self, _text, **_kwargs):
            return None

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            write_task = asyncio.create_task(client.post(
                "/api/transactions",
                headers=headers,
                json={"type": "expense", "amount": 10, "category": "Інше"},
            ))
            await entered_write.wait()
            delete_task = asyncio.create_task(bot.handle_callback(
                SimpleNamespace(callback_query=DeleteQuery()),
                SimpleNamespace(user_data={}),
            ))
            await asyncio.sleep(0)
            release_write.set()
            written, _deleted = await asyncio.gather(write_task, delete_task)
            counts = {
                table: database.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                for table in ("users", "transactions", "user_settings")
            }
            return written.status, counts

    written, counts = run(exercise())

    assert written == 201
    assert counts == {"users": 0, "transactions": 0, "user_settings": 0}


def _insert_recurring(
    database,
    *,
    user_id="user-1",
    kind="expense",
    amount=100,
    currency="UAH",
    amount_uah=100,
    category="Project",
    subcategory="A",
    description="template",
    next_due_date="2026-08-15",
):
    return database.conn.execute(
        """INSERT INTO recurring_operations
           (user_id, type, amount, currency, amount_uah, category, subcategory,
            description, frequency, interval, start_date, anchor_day,
            next_due_date, auto_create, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'monthly', 1, '2026-07-15', 15,
                   ?, 1, 1)""",
        (
            user_id,
            kind,
            amount,
            currency,
            amount_uah,
            category,
            subcategory,
            description,
            next_due_date,
        ),
    ).lastrowid


def test_category_patch_and_subcategory_delete_pause_affected_templates(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    settings = copy.deepcopy(bot.DEFAULT_SETTINGS)
    settings["categories"]["expense"]["Project"] = {
        "emoji": "P",
        "keywords": [],
        "subcategories": ["A", "B"],
    }
    run(database.save_user_settings("user-1", settings))
    first = _insert_recurring(database, subcategory="A")
    second = _insert_recurring(database, subcategory="B")
    database.conn.commit()

    patched = run(bot.api_categories_update(Request(
        body={"subcategories": ["B"]},
        match_info={"type": "expense", "name": "Project"},
    )))
    rows = {
        row["id"]: row["active"]
        for row in run(database.list_recurring_operations("user-1"))
    }
    assert patched.status == 200
    assert rows[first] == 0
    assert rows[second] == 1

    deleted = run(bot.api_subcategories_delete(Request(
        match_info={"type": "expense", "name": "Project", "sub": "B"}
    )))
    rows = {
        row["id"]: row["active"]
        for row in run(database.list_recurring_operations("user-1"))
    }
    assert deleted.status == 204
    assert rows[second] == 0


def test_forecast_uses_same_current_fx_conversion_as_materialization(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    usd_id = _insert_recurring(
        database, kind="income", amount=10, currency="USD", amount_uah=350,
        category="Інше", subcategory=None, description="USD",
    )
    eur_id = _insert_recurring(
        database, kind="expense", amount=10, currency="EUR", amount_uah=400,
        category="Інше", subcategory=None, description="EUR",
    )
    database.conn.commit()

    async def current_rate(currency):
        return {"UAH": 1.0, "USD": 42.0, "EUR": 46.0}[currency]

    monkeypatch.setattr(bot, "get_exchange_rate", current_rate)

    forecast = payload(run(bot.api_forecast(Request(query={
        "year": "2026", "month": "8", "as_of": "2026-08-01",
    }))))
    run(bot.process_due_recurring_operations(through_date="2026-08-15"))
    generated = {
        row["client_request_id"].split(":")[1]: row["amount_uah"]
        for row in run(database.get_transactions("user-1"))
    }

    assert float(forecast["scheduled_income"]) == generated[str(usd_id)] == 420.0
    assert float(forecast["scheduled_expense"]) == generated[str(eur_id)] == 460.0


def test_recurring_suggestions_exclude_generated_rows_and_existing_templates(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)

    async def seed_group(description, *, generated=False):
        for index, day in enumerate(("2026-01-15", "2026-02-15", "2026-03-15")):
            await database.add_transaction(
                "user-1", 100, "UAH", 100, "expense", "Інше",
                description, day, f"{day} 12:00:00",
                client_request_id=(f"recurring:99:{day}" if generated else None),
            )

    async def exercise():
        await seed_group("generated", generated=True)
        await seed_group("existing")
        await seed_group("fresh")
        _insert_recurring(
            database, category="Інше", subcategory=None,
            description="existing",
        )
        database.conn.commit()
        return payload(await bot.api_recurring_suggestions(Request()))

    suggestions = run(exercise())
    assert [item["description"] for item in suggestions] == ["fresh"]
