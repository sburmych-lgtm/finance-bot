import asyncio
import json
from types import SimpleNamespace

import bot


class Request(dict):
    def __init__(self, user_id="user-1", query=None, body=None, match_info=None):
        super().__init__(user_id=user_id, tg_user={"id": user_id})
        self.rel_url = SimpleNamespace(query=query or {})
        self._body = body
        self.match_info = match_info or {}

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "test.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def test_monthly_balance_and_breakdown_use_all_rows_and_isolate_users(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def seed():
        for index in range(20):
            await database.add_transaction(
                "user-1", 10, "UAH", 10, "expense", "Житло", "", 
                "2026-07-10", f"2026-07-10 10:00:{index:02d}",
                subcategory="Комунальні" if index < 12 else None,
            )
        await database.add_transaction(
            "user-1", 500, "UAH", 500, "income", "Інше", "",
            "2026-07-11", "2026-07-11 10:00:00",
        )
        await database.add_transaction(
            "user-2", 999, "UAH", 999, "expense", "Житло", "",
            "2026-07-10", "2026-07-10 10:00:00", subcategory="Комунальні",
        )

    run(seed())
    query = {"year": "2026", "month": "7"}
    balance = payload(run(bot.api_balance(Request(query=query))))
    monthly = payload(run(bot.api_monthly_report(Request(query=query))))
    breakdown = payload(run(bot.api_report_category_breakdown(Request(query={
        **query, "period": "month", "type": "expense", "category": "Житло",
    }))))

    assert balance == {"income": 500.0, "expense": 200.0, "balance": 300.0, "currency": "UAH"}
    assert monthly["transaction_count"] == 21
    assert monthly["total_income"] == balance["income"]
    assert monthly["total_expense"] == balance["expense"]
    assert monthly["expense_by_category"] == {"Житло": 200.0}
    assert breakdown["total"] == monthly["expense_by_category"]["Житло"]
    assert breakdown["breakdown"] == [
        {"name": "Комунальні", "value": 120.0, "percentage": 60.0},
        {"name": "Без підрозділу", "value": 80.0, "percentage": 40.0},
    ]


def test_transaction_rejects_unknown_category_or_wrong_subcategory(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "get_exchange_rate", lambda currency: asyncio.sleep(0, result=1.0))

    unknown = run(bot.api_post_transaction(Request(body={
        "type": "expense", "amount": 10, "category": "Неіснуюча",
    })))
    wrong_sub = run(bot.api_post_transaction(Request(body={
        "type": "expense", "amount": 10, "category": "Інше", "subcategory": "Чужий",
    })))

    assert unknown.status == 400
    assert wrong_sub.status == 400


def test_tax_rejects_non_finite_numbers(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    for field, value in (("single_tax_rate", "NaN"), ("esv_fixed", "Infinity")):
        response = run(bot.api_settings_tax_update(Request(body={field: value})))
        assert response.status == 400


def test_date_validation_rejects_invalid_or_reversed_range():
    assert not bot._looks_like_iso_date("2026-99-99")
    response = run(bot.api_get_transactions(Request(query={
        "from": "2026-07-20", "to": "2026-07-10",
    })))
    assert response.status == 400


def test_category_create_cleans_and_limits_input(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    too_long = run(bot.api_categories_create(Request(body={
        "type": "expense", "name": "x" * 81,
    })))
    bad_subs = run(bot.api_categories_create(Request(body={
        "type": "expense", "name": "Valid", "subcategories": ["A", "A", 1],
    })))

    assert too_long.status == 400
    assert bad_subs.status == 201
    assert payload(bad_subs)["subcategories"] == ["A"]
