"""Cross-feature regression matrix for previously uncovered API and bot paths.

These tests deliberately use an isolated SQLite database and in-memory Telegram
objects.  They never call Telegram, NBU, Railway, or another external service.
"""

import asyncio
import hashlib
import hmac
import inspect
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot
from security_controls import TokenBucketLimiter


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "feature-matrix.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def request(user_id="owner", *, query=None, body=None, match_info=None, tg_user=None):
    class Request(dict):
        def __init__(self):
            super().__init__(
                user_id=str(user_id),
                tg_user=tg_user or {"id": int(user_id) if str(user_id).isdigit() else user_id},
            )
            self.rel_url = SimpleNamespace(query=query or {})
            self.match_info = match_info or {}

        async def json(self):
            return body

    return Request()


def signed_init_data(token, user):
    params = {
        "auth_date": str(int(time.time())),
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def generous_limiter():
    return TokenBucketLimiter(capacity=10_000, refill_rate=10_000)


def configure_api(monkeypatch, token="feature-matrix-token"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "_preauth_limiter", generous_limiter())
    monkeypatch.setattr(bot, "_read_limiter", generous_limiter())
    monkeypatch.setattr(bot, "_write_limiter", generous_limiter())
    monkeypatch.setattr(bot, "_admin_limiter", generous_limiter())
    return token


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})


class FakeQuery:
    def __init__(self, data, user_id=101):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, "kwargs": kwargs})

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, "kwargs": kwargs})


def telegram_update(*, user_id=101, text="", callback_data=None):
    user = SimpleNamespace(
        id=user_id,
        username="ruby_user",
        first_name="Ruby",
        last_name="Tester",
        language_code="uk",
    )
    message = FakeMessage(text)
    query = FakeQuery(callback_data, user_id) if callback_data is not None else None
    return SimpleNamespace(
        effective_user=user,
        message=None if query else message,
        callback_query=query,
    )


def context(**user_data):
    return SimpleNamespace(user_data=dict(user_data), error=None, bot=SimpleNamespace())


def callback_values(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def assert_callback_limit(markup):
    callbacks = callback_values(markup)
    assert callbacks
    assert all(1 <= len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_me_returns_verified_profile_admin_flag_and_registers_user(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    token = configure_api(monkeypatch)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    tg_user = {
        "id": 42,
        "username": "olesia",
        "first_name": "Олеся",
        "last_name": "Ruby",
        "language_code": "uk",
    }

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            response = await client.get(
                "/api/me",
                headers={"X-Telegram-Init-Data": signed_init_data(token, tg_user)},
            )
            return response.status, await response.json(), await database.get_all_users()

    status, body, users = run(exercise())
    assert status == 200
    assert body == {
        "id": "42",
        "username": "olesia",
        "first_name": "Олеся",
        "last_name": "Ruby",
        "is_admin": True,
    }
    assert [(user["user_id"], user["username"]) for user in users] == [("42", "olesia")]


def test_exchange_rates_refresh_only_when_stale_and_keep_last_good_on_failure(monkeypatch):
    calls = []

    async def successful_refresh():
        calls.append("success")
        bot.exchange_rates_cache.update(
            USD=40.25, EUR=43.75, last_update=datetime.now(bot.KYIV_TZ)
        )

    monkeypatch.setattr(bot, "exchange_rates_cache", {
        "USD": 41.5,
        "EUR": 45.2,
        "last_update": datetime.now(bot.KYIV_TZ) - timedelta(hours=2),
    })
    monkeypatch.setattr(bot, "update_exchange_rates", successful_refresh)
    first = payload(run(bot.api_exchange_rates(request())))
    second = payload(run(bot.api_exchange_rates(request())))

    assert calls == ["success"]
    assert first["USD"] == second["USD"] == 40.25
    assert first["EUR"] == second["EUR"] == 43.75
    assert first["updated_at"]

    stale_at = datetime.now(bot.KYIV_TZ) - timedelta(hours=3)
    bot.exchange_rates_cache.update(USD=40.25, EUR=43.75, last_update=stale_at)

    async def failed_refresh_without_destroying_cache():
        calls.append("failed")

    monkeypatch.setattr(bot, "update_exchange_rates", failed_refresh_without_destroying_cache)
    fallback = payload(run(bot.api_exchange_rates(request())))
    assert fallback == {"USD": 40.25, "EUR": 43.75, "updated_at": stale_at.isoformat()}


def test_employee_accounting_and_time_reports_are_complete_and_tenant_isolated(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    employee = "Анна"

    async def seed():
        await bot.update_user_settings("owner", lambda settings: settings["employees"].append(employee))
        await database.add_transaction(
            "owner", 100, "UAH", 100, "income", "Інше", "opening",
            "2026-06-30", "2026-06-30 10:00:00",
        )
        await database.add_transaction(
            "owner", 500, "UAH", 500, "income", f"Від {employee}", "client",
            "2026-07-10", "2026-07-10 10:00:00",
        )
        await database.add_transaction(
            "owner", 200, "UAH", 200, "expense", f"ЗП {employee}", "salary",
            "2026-07-11", "2026-07-11 10:00:00",
        )
        await database.add_transaction(
            "attacker", 9999, "UAH", 9999, "income", f"Від {employee}", "foreign",
            "2026-07-10", "2026-07-10 10:00:00",
        )
        await database.add_time_track(
            "owner", 120, "Робота", "focus", "2026-07-12", "2026-07-12 10:00:00"
        )
        await database.add_time_track(
            "attacker", 600, "Робота", "foreign", "2026-07-12", "2026-07-12 10:00:00"
        )

    run(seed())
    query = {"year": "2026", "month": "7"}
    employees = payload(run(bot.api_report_employees(request(query=query))))
    accounting = payload(run(bot.api_report_accounting(request(query=query))))
    time_report = payload(run(bot.api_report_time(request(query=query))))

    assert employees == [{
        "name": employee,
        "income": 500.0,
        "salary": 200.0,
        "profit": 300.0,
        "roi": 150.0,
    }]
    assert accounting["opening_balance"] == 100.0
    assert accounting["total_income"] == 500.0
    assert accounting["total_expense"] == 200.0
    assert accounting["closing_balance"] == 400.0
    assert accounting["model"] == "simplified_cash_movement"
    assert accounting["disclaimer"]
    assert [(entry["debit"], entry["credit"], entry["amount"]) for entry in accounting["entries"]] == [
        ("—", "701", 500.0),
        ("901", "—", 200.0),
    ]
    assert all(entry["source_class"] == "unclassified" for entry in accounting["entries"])
    assert time_report["total_minutes"] == 120
    assert time_report["by_category"][0]["name"] == "Робота"
    assert time_report["by_category"][0]["percentage"] == 100.0


def test_categories_full_and_subcategory_crud_are_normalized_and_owner_scoped(
    monkeypatch, tmp_path
):
    use_database(monkeypatch, tmp_path)
    created = run(bot.api_categories_create(request(body={
        "type": "expense", "name": "Кава", "emoji": "☕"
    })))
    added = run(bot.api_subcategories_create(request(
        body={"name": "Ранкова"},
        match_info={"type": "expense", "name": "Кава"},
    )))
    owner_full = payload(run(bot.api_categories_full(request())))
    other_full = payload(run(bot.api_categories_full(request("other"))))
    foreign_delete = run(bot.api_subcategories_delete(request(
        "other",
        match_info={"type": "expense", "name": "Кава", "sub": "Ранкова"},
    )))
    deleted = run(bot.api_subcategories_delete(request(
        match_info={"type": "expense", "name": "Кава", "sub": "Ранкова"},
    )))
    after = payload(run(bot.api_categories_full(request())))

    assert created.status == 201
    assert added.status == 201
    assert owner_full["expense"]["Кава"]["subcategories"] == ["Ранкова"]
    assert "Кава" not in other_full["expense"]
    assert foreign_delete.status == 404
    assert deleted.status == 204
    assert after["expense"]["Кава"]["subcategories"] == []
    assert all(
        isinstance(entry["subcategories"], list)
        for category_type in ("expense", "income")
        for entry in after[category_type].values()
    )


def test_employee_crud_rebuilds_categories_and_is_tenant_isolated(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    created = run(bot.api_employees_create(request(body={"name": "Марія"})))
    duplicate = run(bot.api_employees_create(request(body={"name": "Марія"})))
    owner = payload(run(bot.api_employees_list(request())))
    other = payload(run(bot.api_employees_list(request("other"))))
    owner_categories = payload(run(bot.api_categories_full(request())))
    foreign_delete = run(bot.api_employees_delete(request(
        "other", match_info={"name": "Марія"}
    )))
    deleted = run(bot.api_employees_delete(request(match_info={"name": "Марія"})))
    categories_after = payload(run(bot.api_categories_full(request())))

    assert (created.status, duplicate.status) == (201, 409)
    assert owner == ["Марія"]
    assert other == []
    assert "ЗП Марія" in owner_categories["expense"]
    assert "Від Марія" in owner_categories["income"]
    assert foreign_delete.status == 404
    assert deleted.status == 204
    assert "ЗП Марія" not in categories_after["expense"]
    assert "Від Марія" not in categories_after["income"]


def test_time_category_track_list_and_delete_are_owner_scoped(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    created_category = run(bot.api_time_categories_create(request(body={
        "name": "Фокус", "emoji": "🎯"
    })))
    created_track = payload(run(bot.api_time_tracks_create(request(body={
        "minutes": 90,
        "category": "Фокус",
        "description": "deep work",
        "client_request_id": "time-feature-matrix-1",
    }))))
    track_id = created_track["id"]
    other_track_id = run(database.add_time_track(
        "other", 30, "Інше", "foreign", "2026-07-12", "2026-07-12 10:00:00"
    ))

    owner_rows = payload(run(bot.api_time_tracks_list(request())))
    other_rows = payload(run(bot.api_time_tracks_list(request("other"))))
    foreign_delete = run(bot.api_time_tracks_delete(request(
        "other", match_info={"id": str(track_id)}
    )))
    deleted = run(bot.api_time_tracks_delete(request(match_info={"id": str(track_id)})))
    category_deleted = run(bot.api_time_categories_delete(request(
        match_info={"name": "Фокус"}
    )))

    assert created_category.status == 201
    assert [(row["id"], row["minutes"]) for row in owner_rows] == [(track_id, 90)]
    assert [(row["id"], row["minutes"]) for row in other_rows] == [(other_track_id, 30)]
    assert foreign_delete.status == 404
    assert deleted.status == 204
    assert category_deleted.status == 204
    assert payload(run(bot.api_time_tracks_list(request()))) == []


def test_transaction_delete_endpoint_cannot_cross_tenants(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    transaction_id = run(database.add_transaction(
        "owner", 25, "UAH", 25, "expense", "Інше", "",
        "2026-07-12", "2026-07-12 10:00:00",
    ))
    attacked = run(bot.api_delete_transaction(request(
        "attacker", match_info={"id": str(transaction_id)}
    )))
    deleted = run(bot.api_delete_transaction(request(match_info={"id": str(transaction_id)})))
    replay = run(bot.api_delete_transaction(request(match_info={"id": str(transaction_id)})))

    assert attacked.status == 404
    assert deleted.status == 204
    assert replay.status == 404


def test_admin_roster_and_broadcast_history_detail_are_admin_only(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    database.conn.executemany(
        "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (("42", "admin", "Admin"), ("1001", "real", "Real"), ("9990007", "qa", "QA")),
    )
    database.conn.commit()

    async def seed_broadcast():
        broadcast_id = await database.create_broadcast("hello", "2026-07-12 10:00:00")
        await database.save_broadcast_receipts(
            broadcast_id,
            [("1001", "sent", 777, None), ("9990007", "skipped", None, "synthetic")],
            "2026-07-12 10:00:00",
            sent=1,
            failed=0,
            skipped=1,
            total=2,
        )
        return broadcast_id

    broadcast_id = run(seed_broadcast())
    for handler, match_info in (
        (bot.api_admin_users, None),
        (bot.api_admin_broadcasts_list, None),
        (bot.api_admin_broadcast_detail, {"id": str(broadcast_id)}),
    ):
        assert run(handler(request("7", match_info=match_info))).status == 403

    users = payload(run(bot.api_admin_users(request("42"))))
    history = payload(run(bot.api_admin_broadcasts_list(request("42"))))
    detail = payload(run(bot.api_admin_broadcast_detail(request(
        "42", match_info={"id": str(broadcast_id)}
    ))))
    missing = run(bot.api_admin_broadcast_detail(request("42", match_info={"id": "999"})))

    assert users["total"] == 3
    assert users["real_count"] == 2
    assert users["test_count"] == 1
    assert history["broadcasts"][0]["text_preview"] == "hello"
    assert detail["broadcast"]["id"] == broadcast_id
    assert [(row["user_id"], row["status"]) for row in detail["receipts"]] == [
        ("1001", "sent"), ("9990007", "skipped")
    ]
    assert missing.status == 404


def test_telegram_start_info_settings_privacy_terms_and_clear_are_safe(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setenv("MINIAPP_PUBLIC_URL", "https://ruby.example/app/")

    start_update = telegram_update(user_id=101)
    info_update = telegram_update(user_id=101)
    settings_update = telegram_update(user_id=101)
    privacy_update = telegram_update(user_id=101)
    terms_update = telegram_update(user_id=101)
    clear_update = telegram_update(user_id=101)

    async def exercise():
        await bot.start(start_update, context())
        await bot.show_info(info_update, context())
        await bot.show_settings(settings_update, context())
        await bot.privacy_command(privacy_update, context())
        await bot.terms_command(terms_update, context())
        await bot.clear_account_command(clear_update, context())

    run(exercise())
    users = run(database.get_all_users())

    assert [user["user_id"] for user in users] == ["101"]
    assert start_update.message.replies[0]["kwargs"]["reply_markup"]
    assert info_update.message.replies[0]["kwargs"]["reply_markup"]
    assert settings_update.message.replies[0]["kwargs"]["reply_markup"]
    assert "https://ruby.example/app/privacy" in privacy_update.message.replies[0]["text"]
    assert "https://ruby.example/app/terms" in terms_update.message.replies[0]["text"]
    clear_callbacks = callback_values(clear_update.message.replies[0]["kwargs"]["reply_markup"])
    assert clear_callbacks == ["account_delete:confirm", "cancel"]


@pytest.mark.parametrize(
    ("callback_data", "settings_mutator", "expected"),
    [
        (
            "catview:expense:Кава:ранкова ☕",
            lambda settings: settings["categories"]["expense"].update({
                "Кава:ранкова ☕": {"emoji": "☕", "keywords": [], "subcategories": ["З молоком"]}
            }),
            "Кава:ранкова ☕",
        ),
        (
            "emp_view:Марія:Sales",
            lambda settings: settings["employees"].append("Марія:Sales"),
            "Марія:Sales",
        ),
        (
            "emp_view:~Марія:Sales",
            lambda settings: settings["employees"].append("~Марія:Sales"),
            "~Марія:Sales",
        ),
        (
            "timecatview:Глибока:робота",
            lambda settings: settings["time_categories"].update({"Глибока:робота": {"emoji": "🎯"}}),
            "Глибока:робота",
        ),
    ],
)
def test_settings_view_callbacks_preserve_colons_and_unicode(
    monkeypatch, tmp_path, callback_data, settings_mutator, expected
):
    use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings("101", settings_mutator))
    update = telegram_update(user_id=101, callback_data=callback_data)

    run(bot.handle_callback(update, context()))

    assert update.callback_query.answers
    assert update.callback_query.edits, f"{callback_data.split(':', 1)[0]} must render a view"
    assert expected in update.callback_query.edits[-1]["text"]


@pytest.mark.parametrize(
    "markup",
    [
        lambda: bot.get_category_list_keyboard("expense", {
            "expense": {"Дуже:довга українська категорія " + "ї" * 44: {
                "emoji": "🧾", "keywords": [], "subcategories": []
            }},
            "income": {},
        }),
        lambda: bot.get_employee_list_keyboard(["Працівник:відділу " + "ї" * 42]),
        lambda: bot.get_time_category_list_keyboard({
            "Глибока:концентрація " + "ї" * 37: {"emoji": "🎯"}
        }),
    ],
)
def test_all_settings_callback_tokens_fit_telegram_64_byte_limit(markup):
    assert_callback_limit(markup())


def test_all_transaction_callback_tokens_fit_telegram_64_byte_limit():
    category = "Дуже:довга українська категорія " + "ї" * 44
    categories = {
        "expense": {
            category: {"emoji": "🧾", "keywords": [], "subcategories": []},
            f"ЗП {category}": {"emoji": "💼", "keywords": [], "subcategories": []},
        },
        "income": {
            f"Від {category}": {"emoji": "👤", "keywords": [], "subcategories": []}
        },
    }

    markups = (
        bot.get_time_category_keyboard({category: {"emoji": "🎯"}}),
        bot.get_category_keyboard("expense", categories),
        bot.get_category_keyboard("income", categories),
        bot.get_salary_submenu_keyboard([category]),
        bot.get_employee_income_submenu_keyboard([category]),
        bot.get_currency_keyboard("expense", category),
        bot.get_numpad_keyboard("123.45", "expense", category, "UAH"),
    )

    for markup in markups:
        assert_callback_limit(markup)


@pytest.mark.parametrize(
    ("callback_data", "expected", "waiting_for"),
    [
        ("cat:expense:Кава:ранкова", "Кава:ранкова", None),
        ("curr:expense:Кава:ранкова:UAH", "Кава:ранкова", None),
        ("num:expense:Кава:ранкова:UAH:7", "Кава:ранкова", None),
        ("timecat:Глибока:робота", "Глибока:робота", "time_minutes:Глибока:робота"),
    ],
)
def test_transaction_callbacks_preserve_raw_colons(
    monkeypatch, tmp_path, callback_data, expected, waiting_for
):
    use_database(monkeypatch, tmp_path)

    def configure(settings):
        settings["categories"]["expense"]["Кава:ранкова"] = {
            "emoji": "☕", "keywords": [], "subcategories": []
        }
        settings["time_categories"]["Глибока:робота"] = {"emoji": "🎯"}

    run(bot.update_user_settings("101", configure))
    update = telegram_update(user_id=101, callback_data=callback_data)
    ctx = context(amount="")

    run(bot.handle_callback(update, ctx))

    assert update.callback_query.edits
    assert expected in update.callback_query.edits[-1]["text"]
    assert ctx.user_data.get("waiting_for") == waiting_for


def test_generated_opaque_transaction_callback_round_trip(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    category = "Дуже:довга українська категорія " + "ї" * 44

    def configure(settings):
        settings["categories"]["expense"][category] = {
            "emoji": "🧾", "keywords": [], "subcategories": []
        }

    run(bot.update_user_settings("101", configure))
    settings = run(bot.user_settings_for("101"))
    category_markup = bot.get_category_keyboard("expense", settings["categories"])
    category_callback = next(
        button.callback_data
        for row in category_markup.inline_keyboard
        for button in row
        if category in button.text
    )
    category_update = telegram_update(user_id=101, callback_data=category_callback)

    run(bot.handle_callback(category_update, context()))

    assert category in category_update.callback_query.edits[-1]["text"]
    currency_markup = category_update.callback_query.edits[-1]["kwargs"]["reply_markup"]
    currency_callback = next(
        value for value in callback_values(currency_markup) if value.endswith(":UAH")
    )
    currency_update = telegram_update(user_id=101, callback_data=currency_callback)
    ctx = context()

    run(bot.handle_callback(currency_update, ctx))

    assert category in currency_update.callback_query.edits[-1]["text"]
    numpad_markup = currency_update.callback_query.edits[-1]["kwargs"]["reply_markup"]
    numpad_callback = next(
        value for value in callback_values(numpad_markup) if value.endswith(":7")
    )
    numpad_update = telegram_update(user_id=101, callback_data=numpad_callback)

    run(bot.handle_callback(numpad_update, ctx))

    assert category in numpad_update.callback_query.edits[-1]["text"]
    assert ctx.user_data["amount"] == "7"


@pytest.mark.parametrize(
    ("waiting_for", "text", "should_exist"),
    [
        ("employee_name", "П" * 61, lambda settings: "П" * 61 in settings["employees"]),
        (
            "category_name:expense",
            "К" * 81,
            lambda settings: "К" * 81 in settings["categories"]["expense"],
        ),
        (
            "time_category_name",
            "Ч" * 61,
            lambda settings: "Ч" * 61 in settings["time_categories"],
        ),
        ("time_minutes:Інше", "1441", None),
        ("time_minutes:Інше", "1.5", None),
    ],
)
def test_bot_text_inputs_enforce_same_bounds_as_json_api(
    monkeypatch, tmp_path, waiting_for, text, should_exist
):
    database = use_database(monkeypatch, tmp_path)
    update = telegram_update(user_id=101, text=text)
    ctx = context(waiting_for=waiting_for)

    run(bot.handle_text_transaction(update, ctx))
    settings = run(bot.user_settings_for("101"))
    tracks = run(database.get_time_tracks("101"))

    assert update.message.replies
    assert ctx.user_data.get("waiting_for") == waiting_for
    if should_exist is not None:
        assert not should_exist(settings)
    else:
        assert tracks == []


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "inf", "1000000001"])
def test_numpad_save_rejects_non_positive_non_finite_and_oversized_amounts(
    monkeypatch, tmp_path, amount
):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0))
    update = telegram_update(user_id=101, callback_data="num:expense:Інше:UAH:confirm")

    run(bot.save_transaction(update, context(), "expense", "Інше", "UAH", amount))

    assert run(database.get_transactions("101")) == []
    assert update.callback_query.answers
    assert update.callback_query.answers[-1]["kwargs"].get("show_alert") is True


@pytest.mark.parametrize(
    "text",
    ["0 кава", "1000000001 кава", "1e309 кава", "nan кава", "inf кава"],
)
def test_free_text_transactions_reject_invalid_amounts(monkeypatch, tmp_path, text):
    database = use_database(monkeypatch, tmp_path)
    update = telegram_update(user_id=101, text=text)

    run(bot.handle_text_transaction(update, context()))

    assert run(database.get_transactions("101")) == []
    assert update.message.replies


def test_error_handler_logs_exception_and_sends_generic_message_without_secret(monkeypatch):
    handler = getattr(bot, "error_handler", None)
    assert callable(handler), "Telegram Application must register a global error handler"
    assert "application.add_error_handler(error_handler)" in inspect.getsource(bot.main)

    update = telegram_update(user_id=101)
    ctx = context()
    ctx.error = RuntimeError("secret-token-must-not-be-echoed")
    logged = []
    monkeypatch.setattr(bot.logger, "exception", lambda *args, **kwargs: logged.append((args, kwargs)))

    run(handler(update, ctx))

    assert logged
    assert update.message.replies
    assert "secret-token-must-not-be-echoed" not in update.message.replies[-1]["text"]
