import asyncio
import calendar
import copy
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

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
        path="/api/test",
    ):
        super().__init__(user_id=user_id, tg_user={"id": user_id})
        self.rel_url = SimpleNamespace(query=query or {})
        self._body = body
        self.match_info = match_info or {}
        self.method = method
        self.path = path

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body) if response.body else None


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "config.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def settings(*, expenses=("Rent",), incomes=("Sales",), employees=()):
    value = copy.deepcopy(bot.DEFAULT_SETTINGS)
    value["categories"]["expense"] = {
        name: {"emoji": "x", "keywords": [], "subcategories": []}
        for name in expenses
    }
    value["categories"]["income"] = {
        name: {"emoji": "x", "keywords": [], "subcategories": []}
        for name in incomes
    }
    value["employees"] = list(employees)
    return value


def recurring_body(**overrides):
    return {
        "type": "expense",
        "amount": 100,
        "currency": "UAH",
        "category": "Rent",
        "subcategory": None,
        "description": "Office",
        "payment_source": "transfer",
        "frequency": "daily",
        "interval": 1,
        "start_date": "2020-01-01",
        "auto_create": True,
        **overrides,
    }


def test_past_recurring_create_and_reactivation_never_backfill_paused_history(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    today = datetime.now(bot.KYIV_TZ).date()
    past = today - timedelta(days=30)

    created = payload(
        run(
            bot.api_recurring_create(
                Request(body=recurring_body(start_date=past.isoformat()))
            )
        )
    )
    assert date.fromisoformat(created["next_due_date"]) == today
    assert run(bot.process_due_recurring_operations(today))["created"] == 1

    recurring_id = created["id"]
    run(
        bot.api_recurring_patch(
            Request(
                body={"active": False},
                match_info={"id": str(recurring_id)},
                method="PATCH",
            )
        )
    )
    database.conn.execute(
        "UPDATE recurring_operations SET next_due_date = ? WHERE id = ?",
        ((today - timedelta(days=10)).isoformat(), recurring_id),
    )
    database.conn.commit()
    reactivated = payload(
        run(
            bot.api_recurring_patch(
                Request(
                    body={"active": True},
                    match_info={"id": str(recurring_id)},
                    method="PATCH",
                )
            )
        )
    )

    assert date.fromisoformat(reactivated["next_due_date"]) >= today
    before = len(run(database.get_transactions("user-1")))
    run(bot.process_due_recurring_operations(today))
    after = len(run(database.get_transactions("user-1")))
    assert after - before <= 1


def test_monthly_past_start_preserves_anchor_day(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    today = datetime.now(bot.KYIV_TZ).date()
    created = payload(
        run(
            bot.api_recurring_create(
                Request(
                    body=recurring_body(
                        frequency="monthly", start_date="2020-01-31"
                    )
                )
            )
        )
    )
    due = date.fromisoformat(created["next_due_date"])

    assert due >= today
    assert due.day == min(31, calendar.monthrange(due.year, due.month)[1])


def test_active_template_downtime_still_catches_up_pending_dates(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    today = datetime.now(bot.KYIV_TZ).date()
    values, error = run(
        bot._validate_recurring_values(
            "user-1", recurring_body(start_date=today.isoformat())
        )
    )
    assert error is None
    values["next_due_date"] = (today - timedelta(days=3)).isoformat()
    row = run(database.create_recurring_operation("user-1", values))

    result = run(bot.process_due_recurring_operations(today))

    assert result == {"created": 4, "failed": 0, "processed": 1}
    assert len(run(database.get_transactions("user-1"))) == 4
    assert run(database.get_recurring_operation("user-1", row["id"]))[
        "next_due_date"
    ] == (today + timedelta(days=1)).isoformat()


def test_budget_delete_is_owner_scoped_even_after_category_disappears(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    for user in ("user-1", "user-2"):
        run(database.save_user_settings(user, settings(expenses=("Other",))))
        database.conn.execute(
            "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
            "VALUES (?, 'expense', 'Orphan', 100)",
            (user,),
        )
    database.conn.commit()

    deleted = run(
        bot.api_budgets_delete(
            Request(
                match_info={"type": "expense", "category": "Orphan"},
                method="DELETE",
            )
        )
    )

    assert deleted.status == 204
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-2'"
    ).fetchone()[0] == 1


def test_category_delete_cannot_race_budget_recreation(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings(expenses=("Race",))))
    original_upsert = database.upsert_budget

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_upsert(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_upsert(*args, **kwargs)

        monkeypatch.setattr(database, "upsert_budget", delayed_upsert)
        put_task = asyncio.create_task(
            bot.api_budgets_put(
                Request(
                    body={
                        "type": "expense",
                        "category": "Race",
                        "monthly_limit_uah": 100,
                    },
                    method="PUT",
                )
            )
        )
        await entered.wait()
        delete_task = asyncio.create_task(
            bot.api_categories_delete(
                Request(
                    body={},
                    match_info={"type": "expense", "name": "Race"},
                    method="DELETE",
                )
            )
        )
        await asyncio.sleep(0)
        assert not delete_task.done()
        release.set()
        await put_task
        await delete_task

    run(exercise())
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("category_type", "name"),
    [("income", "Від Fake"), ("expense", "ЗП Fake")],
)
def test_employee_category_namespace_cannot_be_manually_created(
    monkeypatch, tmp_path, category_type, name
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings()))
    response = run(
        bot.api_categories_create(
            Request(body={"type": category_type, "name": name}, method="POST")
        )
    )
    assert response.status == 400


def test_generated_employee_categories_cannot_be_renamed_or_deleted(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings(employees=("Alice",))))

    rename = run(
        bot.api_categories_update(
            Request(
                body={"new_name": "Client"},
                match_info={"type": "income", "name": "Від Alice"},
                method="PATCH",
            )
        )
    )
    delete = run(
        bot.api_categories_delete(
            Request(
                match_info={"type": "expense", "name": "ЗП Alice"},
                method="DELETE",
            )
        )
    )
    into_namespace = run(
        bot.api_categories_update(
            Request(
                body={"new_name": "ЗП Fake"},
                match_info={"type": "expense", "name": "Rent"},
                method="PATCH",
            )
        )
    )

    assert rename.status == 400
    assert delete.status == 400
    assert into_namespace.status == 400


def test_employee_delete_cleans_dependencies_but_retains_history(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    for user in ("user-1", "user-2"):
        run(database.save_user_settings(user, settings(employees=("Alice",))))
        for category_type, category in (
            ("income", "Від Alice"),
            ("expense", "ЗП Alice"),
        ):
            database.conn.execute(
                "INSERT INTO budgets "
                "(user_id, type, category, monthly_limit_uah) VALUES (?, ?, ?, 100)",
                (user, category_type, category),
            )
            database.conn.execute(
                """INSERT INTO recurring_operations (
                    user_id, type, amount, currency, amount_uah, category,
                    frequency, interval, start_date, anchor_day, next_due_date,
                    auto_create, active
                ) VALUES (?, ?, 100, 'UAH', 100, ?, 'monthly', 1,
                          '2026-07-01', 1, '2026-07-01', 1, 1)""",
                (user, category_type, category),
            )
            database.conn.commit()
            run(
                database.add_transaction(
                    user, 100, "UAH", 100, category_type, category, "",
                    "2026-07-01", "2026-07-01 10:00:00",
                )
            )

    response = run(
        bot.api_employees_delete(
            Request(
                match_info={"name": "Alice"},
                method="DELETE",
                path="/api/employees/Alice",
            )
        )
    )

    assert response.status == 204
    saved = run(database.get_user_settings("user-1"))
    assert "Alice" not in saved["employees"]
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0
    assert database.conn.execute(
        "SELECT COUNT(*) FROM recurring_operations "
        "WHERE user_id='user-1' AND active=1"
    ).fetchone()[0] == 0
    assert len(run(database.get_transactions("user-1"))) == 2
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-2'"
    ).fetchone()[0] == 2
    assert database.conn.execute(
        "SELECT COUNT(*) FROM recurring_operations "
        "WHERE user_id='user-2' AND active=1"
    ).fetchone()[0] == 2
    report = payload(
        run(
            bot.api_report_employees(
                Request(query={"year": "2026", "month": "7"})
            )
        )
    )
    assert [employee["name"] for employee in report] == ["Alice"]


def test_all_settings_mutation_handlers_are_serialized():
    handlers = (
        bot.api_categories_create,
        bot.api_categories_update,
        bot.api_categories_delete,
        bot.api_subcategories_create,
        bot.api_subcategories_delete,
        bot.api_employees_create,
        bot.api_employees_delete,
        bot.api_time_categories_create,
        bot.api_time_categories_delete,
        bot.api_settings_tax_update,
        bot.api_notification_settings_patch,
        bot.api_settings_reset,
        bot.api_budgets_put,
        bot.api_budgets_delete,
    )
    assert all(hasattr(handler, "__wrapped__") for handler in handlers)


def test_admin_reset_uses_atomic_dependency_aware_reset(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("42", settings()))
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('42', 'expense', 'Rent', 100)"
    )
    database.conn.execute(
        """INSERT INTO recurring_operations (
            user_id, type, amount, currency, amount_uah, category,
            frequency, interval, start_date, anchor_day, next_due_date,
            auto_create, active
        ) VALUES ('42', 'expense', 100, 'UAH', 100, 'Rent', 'monthly', 1,
                  '2026-07-01', 1, '2026-07-01', 1, 1)"""
    )
    database.conn.commit()
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    replies = []

    class Message:
        text = "/reset_user_settings me"

        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42), message=Message()
    )
    run(bot.admin_reset_user_settings(update, SimpleNamespace()))

    assert replies
    assert run(database.get_user_settings("42")) is None
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='42'"
    ).fetchone()[0] == 0
    assert database.conn.execute(
        "SELECT active FROM recurring_operations WHERE user_id='42'"
    ).fetchone()[0] == 0


def test_in_chat_employee_delete_helper_uses_same_dependencies(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", settings(employees=("Alice",))))
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'ЗП Alice', 100)"
    )
    database.conn.commit()

    removed = run(bot.delete_employee_for_user("user-1", "Alice"))

    assert removed is True
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0
