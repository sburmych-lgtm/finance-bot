import asyncio
import calendar
import copy
import json
from datetime import date, datetime
from types import SimpleNamespace

import bot


class Request(dict):
    def __init__(
        self,
        user_id="user-1",
        *,
        query=None,
        body=None,
        match_info=None,
        method="GET",
    ):
        super().__init__(user_id=user_id, tg_user={"id": user_id})
        self.rel_url = SimpleNamespace(query=query or {})
        self._body = body
        self.match_info = match_info or {}
        self.method = method
        self.path = "/api/test"

    async def json(self):
        return self._body


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, *, chat_id, text, **_kwargs):
        self.messages.append((str(chat_id), text))
        return SimpleNamespace(message_id=len(self.messages))


class FlakyBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def send_message(self, *, chat_id, text, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary Telegram failure")
        return await super().send_message(chat_id=chat_id, text=text, **kwargs)


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body) if response.body else None


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "block2-automation.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def settings(*, expenses=("Оренда", "Кава"), incomes=("Клієнт A",)):
    value = copy.deepcopy(bot.DEFAULT_SETTINGS)
    value["categories"]["expense"] = {
        name: {"emoji": "•", "keywords": [], "subcategories": []}
        for name in expenses
    }
    value["categories"]["income"] = {
        name: {"emoji": "•", "keywords": [], "subcategories": []}
        for name in incomes
    }
    value["tax_config"]["group"] = "none"
    value["tax_config"]["profiles_by_year"][
        str(bot.CURRENT_TAX_RULES_YEAR)
    ]["group"] = "none"
    return value


def recurring_body(**overrides):
    return {
        "type": "expense",
        "amount": 1200,
        "currency": "UAH",
        "category": "Оренда",
        "subcategory": None,
        "description": "Офіс",
        "payment_source": "transfer",
        "frequency": "monthly",
        "interval": 1,
        "start_date": "2026-01-31",
        "auto_create": True,
        **overrides,
    }


def test_automation_schema_is_idempotent_and_account_deletion_is_complete(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    database = bot.Database(database.db_file)
    tables = {
        row[0]
        for row in database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "recurring_operations",
        "notification_preferences",
        "notification_deliveries",
    } <= tables

    database.conn.execute(
        "INSERT INTO notification_preferences "
        "(user_id, weekly_digest_enabled) VALUES ('user-1', 1)"
    )
    database.conn.execute(
        "INSERT INTO notification_deliveries "
        "(user_id, kind, period_key, status) "
        "VALUES ('user-1', 'weekly_digest', '2026-W28', 'sent')"
    )
    database.conn.commit()
    result = run(database.delete_user_account("user-1"))
    assert result["deleted_rows"]["notification_preferences"] == 1
    assert result["deleted_rows"]["notification_deliveries"] == 1


def test_recurring_crud_is_validated_and_owner_scoped(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    run(database.save_user_settings("user-2", settings(expenses=("Інше",))))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )

    created_response = run(bot.api_recurring_create(Request(body=recurring_body())))
    created = payload(created_response)
    recurring_id = created["id"]

    assert created_response.status == 201
    first_due = date.fromisoformat(created["next_due_date"])
    assert first_due >= datetime.now(bot.KYIV_TZ).date()
    assert first_due.day == calendar.monthrange(
        first_due.year, first_due.month
    )[1]
    assert created["payment_source"] == "transfer"
    assert payload(run(bot.api_recurring_list(Request()))) == [created]
    assert payload(run(bot.api_recurring_list(Request("user-2")))) == []

    foreign = run(
        bot.api_recurring_patch(
            Request(
                "user-2",
                body={"active": False},
                match_info={"id": str(recurring_id)},
                method="PATCH",
            )
        )
    )
    invalid = run(
        bot.api_recurring_create(
            Request(body=recurring_body(frequency="sometimes"))
        )
    )
    updated = payload(
        run(
            bot.api_recurring_patch(
                Request(
                    body={"amount": 1300, "active": False},
                    match_info={"id": str(recurring_id)},
                    method="PATCH",
                )
            )
        )
    )
    assert foreign.status == 404
    assert invalid.status == 400
    assert updated["amount"] == 1300
    assert updated["active"] is False

    deleted = run(
        bot.api_recurring_delete(
            Request(
                match_info={"id": str(recurring_id)},
                method="DELETE",
            )
        )
    )
    assert deleted.status == 204
    assert payload(run(bot.api_recurring_list(Request()))) == []


def test_due_recurring_generation_is_month_end_safe_and_idempotent(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    created = payload(
        run(bot.api_recurring_create(Request(body=recurring_body())))
    )
    database.conn.execute(
        "UPDATE recurring_operations SET next_due_date='2026-01-31' WHERE id=?",
        (created["id"],),
    )
    database.conn.commit()

    first = run(bot.process_due_recurring_operations(date(2026, 3, 31)))
    second = run(bot.process_due_recurring_operations(date(2026, 3, 31)))
    rows = run(database.get_transactions("user-1"))
    refreshed = payload(run(bot.api_recurring_list(Request())))[0]

    assert first == {"created": 3, "failed": 0, "processed": 1}
    assert second == {"created": 0, "failed": 0, "processed": 0}
    assert {row["date"] for row in rows} == {
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
    }
    assert {row["payment_source"] for row in rows} == {"transfer"}
    assert len({row["client_request_id"] for row in rows}) == 3
    assert refreshed["id"] == created["id"]
    assert refreshed["next_due_date"] == "2026-04-30"


def test_recurring_category_rename_cascades_and_delete_pauses_template(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    recurring = payload(
        run(bot.api_recurring_create(Request(body=recurring_body())))
    )

    renamed = run(
        bot.api_categories_update(
            Request(
                body={"new_name": "Офіс"},
                match_info={"type": "expense", "name": "Оренда"},
                method="PATCH",
            )
        )
    )
    assert renamed.status == 200
    assert payload(run(bot.api_recurring_list(Request())))[0]["category"] == "Офіс"

    deleted = run(
        bot.api_categories_delete(
            Request(
                match_info={"type": "expense", "name": "Офіс"},
                method="DELETE",
            )
        )
    )
    paused = payload(run(bot.api_recurring_list(Request())))[0]
    assert deleted.status == 204
    assert paused["id"] == recurring["id"]
    assert paused["active"] is False


def test_recurring_suggestions_are_user_isolated(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    for user, amount in (("user-1", 1200), ("user-2", 9999)):
        for month, day in ((1, 31), (2, 28), (3, 31)):
            run(
                database.add_transaction(
                    user,
                    amount,
                    "UAH",
                    amount,
                    "expense",
                    "Оренда",
                    "Офіс",
                    f"2026-{month:02d}-{day:02d}",
                    f"2026-{month:02d}-{day:02d} 10:00:00",
                    payment_source="transfer",
                )
            )

    suggestions = payload(run(bot.api_recurring_suggestions(Request())))

    assert len(suggestions) == 1
    assert suggestions[0]["amount_uah"] == "1200.00"
    assert "9999" not in json.dumps(suggestions)


def test_insights_digest_and_forecast_use_full_user_month(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'Кава', 1000)"
    )
    database.conn.commit()
    rows = [
        ("expense", 450, "Кава", "2026-07-06"),
        ("expense", 450, "Кава", "2026-07-07"),
        ("expense", 300, "Кава", "2026-06-29"),
        ("expense", 300, "Кава", "2026-06-30"),
        ("income", 10000, "Клієнт A", "2026-07-08"),
    ]
    for tx_type, amount, category, day in rows:
        run(
            database.add_transaction(
                "user-1", amount, "UAH", amount, tx_type, category, "", day,
                f"{day} 10:00:00",
            )
        )
    run(
        database.save_user_settings(
            "user-1", settings(expenses=("Оренда", "Кава"))
        )
    )
    run(
        bot.api_recurring_create(
            Request(
                body=recurring_body(
                    amount=1500, start_date="2026-07-25"
                )
            )
        )
    )

    insights = payload(
        run(bot.api_insights(Request(query={"as_of": "2026-07-12"})))
    )
    digest = payload(
        run(bot.api_weekly_digest(Request(query={"week_start": "2026-07-06"})))
    )
    forecast = payload(
        run(
            bot.api_forecast(
                Request(query={"year": "2026", "month": "7", "as_of": "2026-07-12"})
            )
        )
    )

    assert {item["kind"] for item in insights} >= {
        "budget_warning",
        "weekly_category_change",
        "income_concentration",
    }
    assert digest["total_income"] == "10000.00"
    assert digest["total_expense"] == "900.00"
    assert forecast["current_net"] == "9100.00"
    assert forecast["scheduled_expense"] == "1500.00"
    assert forecast["projected_result_after_tax"] == "7600.00"
    assert forecast["basis"] == "recorded_plus_scheduled"


def test_weekly_digest_preferences_and_delivery_are_opt_in_and_idempotent(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    fake_bot = FakeBot()
    for user in ("1001", "1002"):
        database.conn.execute(
            "INSERT INTO users (user_id, first_name) VALUES (?, 'Test')", (user,)
        )
    database.conn.commit()

    default = payload(run(bot.api_notification_settings(Request("1001"))))
    enabled = payload(
        run(
            bot.api_notification_settings_patch(
                Request(
                    "1001",
                    body={"weekly_digest_enabled": True},
                    method="PATCH",
                )
            )
        )
    )
    assert default["weekly_digest_enabled"] is False
    assert enabled["weekly_digest_enabled"] is True

    first = run(
        bot.send_weekly_digests(
            fake_bot, week_start=date(2026, 7, 6)
        )
    )
    second = run(
        bot.send_weekly_digests(
            fake_bot, week_start=date(2026, 7, 6)
        )
    )
    assert first == {"sent": 1, "failed": 0, "skipped": 0}
    assert second == {"sent": 0, "failed": 0, "skipped": 1}
    assert [recipient for recipient, _text in fake_bot.messages] == ["1001"]


def test_block2_automation_routes_are_registered():
    app = bot.build_api_app()
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}
    assert {
        ("GET", "/api/recurring-operations"),
        ("POST", "/api/recurring-operations"),
        ("PATCH", "/api/recurring-operations/{id}"),
        ("DELETE", "/api/recurring-operations/{id}"),
        ("GET", "/api/recurring-suggestions"),
        ("GET", "/api/insights"),
        ("GET", "/api/digest/weekly"),
        ("GET", "/api/forecast"),
        ("GET", "/api/settings/notifications"),
        ("PATCH", "/api/settings/notifications"),
    } <= routes


def test_failed_and_stale_digest_claims_retry_but_sent_never_duplicates(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.set_notification_preferences("1001", True))
    flaky = FlakyBot()

    failed = run(bot.send_weekly_digests(flaky, week_start=date(2026, 7, 6)))
    retried = run(bot.send_weekly_digests(flaky, week_start=date(2026, 7, 6)))
    duplicate = run(bot.send_weekly_digests(flaky, week_start=date(2026, 7, 6)))

    assert failed == {"sent": 0, "failed": 1, "skipped": 0}
    assert retried == {"sent": 1, "failed": 0, "skipped": 0}
    assert duplicate == {"sent": 0, "failed": 0, "skipped": 1}
    assert flaky.attempts == 2

    database.conn.execute(
        "INSERT INTO notification_deliveries "
        "(user_id, kind, period_key, status, updated_at) "
        "VALUES ('1001', 'weekly_digest', '2026-W29', 'processing', "
        "'2020-01-01 00:00:00')"
    )
    database.conn.commit()
    stale_retry = run(
        bot.send_weekly_digests(FakeBot(), week_start=date(2026, 7, 13))
    )
    assert stale_retry == {"sent": 1, "failed": 0, "skipped": 0}


def test_sunday_digest_job_sends_current_monday_to_sunday_week(monkeypatch):
    captured = []

    async def fake_send(_telegram_bot, *, week_start):
        captured.append(week_start)
        return {"sent": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(bot, "send_weekly_digests", fake_send)
    context = SimpleNamespace(bot=FakeBot())
    run(bot.weekly_digest_job(context, today=date(2026, 7, 12)))

    assert captured == [date(2026, 7, 6)]


def test_schedule_patch_preserves_progress_and_never_backfills_to_today(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    created = payload(run(bot.api_recurring_create(Request(body=recurring_body()))))
    database.conn.execute(
        "UPDATE recurring_operations SET next_due_date='2026-01-31' WHERE id=?",
        (created["id"],),
    )
    database.conn.commit()
    run(bot.process_due_recurring_operations(date(2026, 3, 31)))

    patched = payload(
        run(
            bot.api_recurring_patch(
                Request(
                    body={"frequency": "weekly", "interval": 1},
                    match_info={"id": str(created["id"])},
                    method="PATCH",
                )
            )
        )
    )
    today = datetime.now(bot.KYIV_TZ).date()

    assert patched["last_generated_date"] == "2026-03-31"
    assert date.fromisoformat(patched["next_due_date"]) > today
    assert run(bot.process_due_recurring_operations(today)) == {
        "created": 0,
        "failed": 0,
        "processed": 0,
    }


def test_account_delete_coordinates_with_inflight_recurring_generation(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    created = payload(run(bot.api_recurring_create(Request(body=recurring_body()))))
    database.conn.execute(
        "UPDATE recurring_operations SET next_due_date='2026-01-31' WHERE id=?",
        (created["id"],),
    )
    database.conn.commit()

    async def exercise():
        rate_started = asyncio.Event()
        release_rate = asyncio.Event()

        async def delayed_rate(_currency):
            rate_started.set()
            await release_rate.wait()
            return 1.0

        monkeypatch.setattr(bot, "get_exchange_rate", delayed_rate)
        processing = asyncio.create_task(
            bot.process_due_recurring_operations(date(2026, 1, 31))
        )
        await rate_started.wait()
        deletion = asyncio.create_task(database.delete_user_account("user-1"))
        await asyncio.sleep(0)
        assert not deletion.done()
        release_rate.set()
        await processing
        await deletion

    run(exercise())

    assert run(database.get_transactions("user-1")) == []
    assert run(database.list_recurring_operations("user-1")) == []
