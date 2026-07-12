import asyncio
import copy
import json
import sqlite3
from datetime import datetime
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
    if not response.body:
        return None
    return json.loads(response.body)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "block2.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def user_settings(*, expense=("Food",), income=("Sales",)):
    settings = copy.deepcopy(bot.DEFAULT_SETTINGS)
    settings["categories"]["expense"] = {
        name: {"emoji": "x", "keywords": [], "subcategories": []}
        for name in expense
    }
    settings["categories"]["income"] = {
        name: {"emoji": "x", "keywords": [], "subcategories": []}
        for name in income
    }
    return settings


def transaction_body(**overrides):
    return {
        "type": "expense",
        "amount": 40,
        "currency": "UAH",
        "category": "Food",
        "subcategory": None,
        "description": "Lunch",
        **overrides,
    }


def month_query():
    now = datetime.now(bot.KYIV_TZ)
    return {"year": str(now.year), "month": str(now.month)}


def test_payment_source_migration_is_nullable_and_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'UAH',
            amount_uah REAL NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date DATE NOT NULL,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.execute(
        """INSERT INTO transactions
           (user_id, amount, amount_uah, type, category, date, timestamp)
           VALUES ('legacy', 10, 10, 'expense', 'Cash-like name',
                   '2026-07-01', '2026-07-01 10:00:00')"""
    )
    connection.commit()
    connection.close()

    first = bot.Database(str(path))
    second = bot.Database(str(path))

    columns = {
        row[1] for row in second.conn.execute("PRAGMA table_info(transactions)")
    }
    row = second.conn.execute(
        "SELECT payment_source FROM transactions WHERE user_id='legacy'"
    ).fetchone()

    assert "payment_source" in columns
    assert row[0] is None
    assert second.conn.execute(
        "SELECT COUNT(*) FROM _migrations "
        "WHERE name='20260712_add_payment_source'"
    ).fetchone()[0] == 1
    assert first.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budgets'"
    ).fetchone()


def test_payment_source_roundtrip_idempotency_templates_and_reports(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings()))
    monkeypatch.setattr(
        bot,
        "get_exchange_rate",
        lambda _currency: asyncio.sleep(0, result=1.0),
    )
    body = transaction_body(
        payment_source="cash", client_request_id="source-roundtrip-1"
    )

    created_response = run(bot.api_post_transaction(Request(body=body)))
    created = payload(created_response)
    replay = payload(run(bot.api_post_transaction(Request(body=body))))
    conflict = run(
        bot.api_post_transaction(
            Request(body={**body, "payment_source": "card"})
        )
    )

    now = datetime.now(bot.KYIV_TZ)
    run(
        database.add_transaction(
            "user-1",
            10,
            "UAH",
            10,
            "expense",
            "Food",
            "Legacy cash-looking row",
            now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d 00:00:00"),
        )
    )
    run(
        database.add_transaction(
            "user-2",
            999,
            "UAH",
            999,
            "expense",
            "Food",
            "Other tenant",
            now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d 23:59:59"),
            payment_source="transfer",
        )
    )

    listed = payload(
        run(bot.api_get_transactions(Request(query={"limit": "all"})))
    )
    quick = payload(run(bot.api_quick_templates(Request(query={"limit": "10"}))))
    monthly = payload(run(bot.api_monthly_report(Request(query=month_query()))))
    sources = payload(
        run(bot.api_report_payment_sources(Request(query=month_query()))))

    assert created_response.status == 201
    assert created["payment_source"] == "cash"
    assert replay["duplicate"] is True
    assert replay["payment_source"] == "cash"
    assert conflict.status == 409
    assert {item["payment_source"] for item in listed} == {"cash", None}
    assert quick["last_operation"]["payment_source"] == "cash"
    assert {item["payment_source"] for item in quick["templates"]} == {
        "cash",
        None,
    }
    assert monthly["expense_by_payment_source"] == {
        "cash": 40.0,
        "card": 0.0,
        "transfer": 0.0,
        "other": 0.0,
        "unclassified": 10.0,
    }
    assert sources["expense_by_payment_source"] == (
        monthly["expense_by_payment_source"]
    )
    assert sources["total_expense"] == 50.0
    assert "Other tenant" not in json.dumps(sources)


@pytest.mark.parametrize("invalid", ["", "Cash", "bank", 1, [], True])
def test_post_rejects_invalid_payment_source(monkeypatch, tmp_path, invalid):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings()))

    response = run(
        bot.api_post_transaction(
            Request(body=transaction_body(payment_source=invalid))
        )
    )

    assert response.status == 400
    assert "payment_source" in payload(response)["detail"]
    assert run(database.get_transactions("user-1")) == []


def test_patch_payment_source_is_owner_scoped_and_nullable(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    tx_id = run(
        database.add_transaction(
            "user-1",
            20,
            "UAH",
            20,
            "expense",
            "Food",
            "Taxi",
            "2026-07-01",
            "2026-07-01 10:00:00",
        )
    )

    foreign = run(
        bot.api_patch_transaction(
            Request(
                "user-2",
                body={"payment_source": "card"},
                match_info={"id": str(tx_id)},
                method="PATCH",
            )
        )
    )
    invalid = run(
        bot.api_patch_transaction(
            Request(
                body={"payment_source": "bank"},
                match_info={"id": str(tx_id)},
                method="PATCH",
            )
        )
    )
    updated = payload(
        run(
            bot.api_patch_transaction(
                Request(
                    body={"payment_source": "card"},
                    match_info={"id": str(tx_id)},
                    method="PATCH",
                )
            )
        )
    )
    cleared = payload(
        run(
            bot.api_patch_transaction(
                Request(
                    body={"payment_source": None},
                    match_info={"id": str(tx_id)},
                    method="PATCH",
                )
            )
        )
    )

    assert foreign.status == 404
    assert invalid.status == 400
    assert updated["id"] == tx_id
    assert updated["payment_source"] == "card"
    assert cleared["payment_source"] is None


def test_budgets_progress_rounding_and_tenant_isolation(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Food",))))
    run(database.save_user_settings("user-2", user_settings(expense=("Travel",))))

    created = payload(
        run(
            bot.api_budgets_put(
                Request(
                    body={
                        "type": "expense",
                        "category": "Food",
                        "monthly_limit_uah": "100.005",
                    },
                    method="PUT",
                )
            )
        )
    )
    repeated = payload(
        run(
            bot.api_budgets_put(
                Request(
                    body={
                        "type": "expense",
                        "category": "Food",
                        "monthly_limit_uah": 100.005,
                    },
                    method="PUT",
                )
            )
        )
    )
    owner_only = run(
        bot.api_budgets_put(
            Request(
                "user-2",
                body={
                    "type": "expense",
                    "category": "Food",
                    "monthly_limit_uah": 500,
                },
                method="PUT",
            )
        )
    )

    for amount in (25.55, 74.46):
        run(
            database.add_transaction(
                "user-1",
                amount,
                "UAH",
                amount,
                "expense",
                "Food",
                "",
                "2026-07-01",
                "2026-07-01 10:00:00",
            )
        )
    run(
        database.add_transaction(
            "user-1", 500, "UAH", 500, "expense", "Food", "",
            "2026-06-30", "2026-06-30 10:00:00",
        )
    )
    run(
        database.add_transaction(
            "user-2", 999, "UAH", 999, "expense", "Food", "",
            "2026-07-01", "2026-07-01 10:00:00",
        )
    )

    progress = payload(
        run(
            bot.api_budgets_get(
                Request(query={"year": "2026", "month": "7"})
            )
        )
    )
    foreign_progress = payload(
        run(
            bot.api_budgets_get(
                Request("user-2", query={"year": "2026", "month": "7"})
            )
        )
    )

    assert created["monthly_limit_uah"] == 100.01
    assert repeated["monthly_limit_uah"] == 100.01
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 1
    assert owner_only.status == 400
    assert foreign_progress["budgets"] == []
    assert progress["budgets"] == [
        {
            "type": "expense",
            "category": "Food",
            "monthly_limit_uah": 100.01,
            "spent_uah": 100.01,
            "remaining_uah": 0.0,
            "progress_percent": 100.0,
            "is_exceeded": False,
        }
    ]

    foreign_delete = run(
        bot.api_budgets_delete(
            Request(
                "user-2",
                match_info={"type": "expense", "category": "Food"},
                method="DELETE",
            )
        )
    )
    assert foreign_delete.status in (400, 404)
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 1


def test_category_rename_cascades_and_delete_preserves_history(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Old",))))
    run(database.save_user_settings("user-2", user_settings(expense=("Old",))))
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'Old', 100)"
    )
    database.conn.commit()
    for user in ("user-1", "user-2"):
        run(
            database.add_transaction(
                user, 30, "UAH", 30, "expense", "Old", "", "2026-07-01",
                "2026-07-01 10:00:00",
            )
        )

    renamed = run(
        bot.api_categories_update(
            Request(
                body={"new_name": "New"},
                match_info={"type": "expense", "name": "Old"},
                method="PATCH",
            )
        )
    )

    owner_tx = run(database.get_transactions("user-1"))[0]
    other_tx = run(database.get_transactions("user-2"))[0]
    owner_settings = run(database.get_user_settings("user-1"))
    budget = database.conn.execute(
        "SELECT category FROM budgets WHERE user_id='user-1'"
    ).fetchone()

    assert renamed.status == 200
    assert owner_tx["category"] == "New"
    assert other_tx["category"] == "Old"
    assert budget[0] == "New"
    assert "New" in owner_settings["categories"]["expense"]
    assert "Old" not in owner_settings["categories"]["expense"]

    deleted = run(
        bot.api_categories_delete(
            Request(
                match_info={"type": "expense", "name": "New"},
                method="DELETE",
            )
        )
    )

    assert deleted.status == 204
    assert run(database.get_transactions("user-1"))[0]["category"] == "New"
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0


def test_category_rename_rolls_back_all_dependencies_on_conflict(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Old",))))
    database.conn.executemany(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', ?, 100)",
        [("Old",), ("New",)],
    )
    database.conn.commit()
    run(
        database.add_transaction(
            "user-1", 30, "UAH", 30, "expense", "Old", "", "2026-07-01",
            "2026-07-01 10:00:00",
        )
    )

    response = run(
        bot.api_categories_update(
            Request(
                body={"new_name": "New"},
                match_info={"type": "expense", "name": "Old"},
                method="PATCH",
            )
        )
    )

    settings = run(database.get_user_settings("user-1"))
    categories = [
        row[0]
        for row in database.conn.execute(
            "SELECT category FROM budgets WHERE user_id='user-1' ORDER BY category"
        )
    ]
    assert response.status == 409
    assert run(database.get_transactions("user-1"))[0]["category"] == "Old"
    assert categories == ["New", "Old"]
    assert "Old" in settings["categories"]["expense"]
    assert "New" not in settings["categories"]["expense"]


def test_account_deletion_removes_budgets(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'Food', 100)"
    )
    database.conn.commit()

    result = run(database.delete_user_account("user-1"))

    assert result["deleted_rows"]["budgets"] == 1
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0


def test_block2_routes_are_registered():
    app = bot.build_api_app()
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
    }

    assert ("PATCH", "/api/transactions/{id}") in routes
    assert ("GET", "/api/reports/payment-sources") in routes
    assert ("GET", "/api/budgets") in routes
    assert ("PUT", "/api/budgets") in routes
    assert ("DELETE", "/api/budgets/{type}/{category}") in routes


def test_idempotency_fingerprint_survives_source_and_category_corrections(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Old",))))
    monkeypatch.setattr(
        bot, "get_exchange_rate", lambda _currency: asyncio.sleep(0, result=1.0)
    )
    original = transaction_body(
        category="Old",
        payment_source="cash",
        client_request_id="immutable-request-1",
    )
    created = payload(run(bot.api_post_transaction(Request(body=original))))
    run(
        bot.api_patch_transaction(
            Request(
                body={"payment_source": "card"},
                match_info={"id": str(created["id"])},
                method="PATCH",
            )
        )
    )
    run(
        bot.api_categories_update(
            Request(
                body={"new_name": "New"},
                match_info={"type": "expense", "name": "Old"},
                method="PATCH",
            )
        )
    )

    original_replay = run(bot.api_post_transaction(Request(body=original)))
    mutated_replay = run(
        bot.api_post_transaction(
            Request(body={**original, "category": "New", "payment_source": "card"})
        )
    )

    assert original_replay.status == 200
    assert payload(original_replay)["duplicate"] is True
    assert payload(original_replay)["category"] == "New"
    assert payload(original_replay)["payment_source"] == "card"
    assert mutated_replay.status == 409
    assert len(run(database.get_transactions("user-1"))) == 1


def test_concurrent_category_renames_do_not_lose_settings(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("A", "B"))))
    original_settings_for = bot.user_settings_for
    active_reads = 0
    max_active_reads = 0

    async def delayed_settings_for(user_id):
        nonlocal active_reads, max_active_reads
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        try:
            await asyncio.sleep(0.02)
            return await original_settings_for(user_id)
        finally:
            active_reads -= 1

    monkeypatch.setattr(bot, "user_settings_for", delayed_settings_for)

    async def rename(old, new):
        return await bot.api_categories_update(
            Request(
                body={"new_name": new},
                match_info={"type": "expense", "name": old},
                method="PATCH",
            )
        )

    async def exercise():
        return await asyncio.gather(rename("A", "A1"), rename("B", "B1"))

    responses = run(exercise())
    saved = run(database.get_user_settings("user-1"))

    assert [response.status for response in responses] == [200, 200]
    assert set(saved["categories"]["expense"]) == {"A1", "B1"}
    assert max_active_reads == 1


def test_settings_reset_atomically_removes_custom_budgets(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Custom",))))
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'Custom', 100)"
    )
    database.conn.commit()

    response = run(bot.api_settings_reset(Request(method="DELETE")))

    assert response.status == 200
    assert database.conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id='user-1'"
    ).fetchone()[0] == 0
    assert run(database.get_user_settings("user-1")) is None


def test_budget_progress_quantizes_sqlite_real_before_comparison(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(database.save_user_settings("user-1", user_settings(expense=("Food",))))
    database.conn.execute(
        "INSERT INTO budgets (user_id, type, category, monthly_limit_uah) "
        "VALUES ('user-1', 'expense', 'Food', 0.30)"
    )
    database.conn.commit()
    for amount in (0.1, 0.2):
        run(
            database.add_transaction(
                "user-1", amount, "UAH", amount, "expense", "Food", "",
                "2026-07-01", "2026-07-01 10:00:00",
            )
        )

    result = payload(
        run(
            bot.api_budgets_get(
                Request(query={"year": "2026", "month": "7"})
            )
        )
    )["budgets"][0]

    assert result["spent_uah"] == 0.30
    assert result["remaining_uah"] == 0.0
    assert result["progress_percent"] == 100.0
    assert result["is_exceeded"] is False
