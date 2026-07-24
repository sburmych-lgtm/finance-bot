import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

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


def signed_init_data(token, user_id):
    params = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def test_tax_group_calculation_uses_versioned_2025_and_2026_rules():
    none = bot.calculate_tax_group(1000, {"group": "none"}, year=2026)
    assert none["group"] == "none"
    assert none["single_tax"] == 0
    assert none["esv"] == 0
    assert none["military_levy"] == 0
    assert none["total_tax"] == 0

    fop1 = bot.calculate_tax_group(1000, {"group": "fop1"}, year=2026)
    assert fop1["single_tax"] == 332.80
    assert fop1["esv"] == 1902.34
    assert fop1["military_levy"] == 864.70
    assert fop1["total_tax"] == 3099.84

    fop2_2025 = bot.calculate_tax_group(1000, {"group": "fop2"}, year=2025)
    fop2_2026 = bot.calculate_tax_group(1000, {"group": "fop2"}, year=2026)
    assert fop2_2025["single_tax"] == 1600
    assert fop2_2025["esv"] == 1760
    assert fop2_2025["military_levy"] == 800
    assert fop2_2025["total_tax"] == 4160
    assert fop2_2026["single_tax"] == 1729.40
    assert fop2_2026["esv"] == 1902.34
    assert fop2_2026["military_levy"] == 864.70
    assert fop2_2026["total_tax"] == 4496.44


def test_tax_group_3_supports_5_percent_and_3_percent_vat_without_counting_vat():
    standard = bot.calculate_tax_group(
        100_000,
        {"group": "fop3", "scheme": "5_percent"},
        year=2026,
    )
    vat = bot.calculate_tax_group(
        100_000,
        {"group": "fop3", "scheme": "3_percent_vat"},
        year=2026,
    )

    assert standard["single_tax_rate"] == 0.05
    assert standard["single_tax"] == 5000
    assert standard["military_levy"] == 1000
    assert standard["total_tax"] == 7902.34
    assert standard["vat_registered"] is False

    assert vat["single_tax_rate"] == 0.03
    assert vat["single_tax"] == 3000
    assert vat["military_levy"] == 1000
    assert vat["total_tax"] == 5902.34
    assert vat["vat_registered"] is True
    assert vat["vat_included"] is False


def test_tax_profile_overrides_are_scoped_to_report_year():
    config = {
        "group": "fop2",
        "profiles_by_year": {
            "2025": {"group": "none"},
            "2026": {"group": "fop2", "fop2_fixed": 1200},
        },
    }

    assert bot.calculate_tax_group(1000, config, year=2025)["total_tax"] == 0
    result_2026 = bot.calculate_tax_group(1000, config, year=2026)
    assert result_2026["single_tax"] == 1200
    assert result_2026["total_tax"] == 3967.04


def test_tax_settings_update_is_scoped_to_requested_year(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    response_2025 = run(bot.api_settings_tax_update(Request(body={
        "year": 2025,
        "group": "none",
    })))
    response_2026 = run(bot.api_settings_tax_update(Request(body={
        "year": 2026,
        "group": "fop3",
        "scheme": "3_percent_vat",
    })))
    settings = run(database.get_user_settings("user-1"))

    assert response_2025.status == 200
    assert response_2026.status == 200
    assert settings["tax_config"]["profiles_by_year"]["2025"]["group"] == "none"
    assert settings["tax_config"]["profiles_by_year"]["2026"]["group"] == "fop3"
    assert settings["tax_config"]["profiles_by_year"]["2026"]["scheme"] == "3_percent_vat"


def test_settings_returns_fully_resolved_profiles_for_every_supported_tax_year(
    monkeypatch, tmp_path
):
    use_database(monkeypatch, tmp_path)

    result = payload(run(bot.api_settings(Request())))

    assert result["supported_tax_years"] == [2025, 2026]
    assert result["tax_profiles"]["2025"]["esv_fixed"] == 1760.0
    assert result["tax_profiles"]["2026"]["esv_fixed"] == 1902.34
    assert result["tax_profiles"]["2025"]["year"] == 2025
    assert result["tax_profiles"]["2026"]["year"] == 2026


def test_tax_report_exposes_military_levy_and_vat_metadata(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def seed():
        await database.add_transaction(
            "user-1", 100_000, "UAH", 100_000, "income", "Інше", "",
            "2026-07-10", "2026-07-10 10:00:00",
        )
        settings = await bot.user_settings_for("user-1")
        settings["tax_config"] = {
            "profiles_by_year": {
                "2026": {"group": "fop3", "scheme": "3_percent_vat"},
            },
        }
        await bot.save_user_settings("user-1", settings)

    run(seed())
    result = payload(run(bot.api_report_tax(Request(query={"year": "2026", "month": "7"}))))

    assert result["single_tax"] == 3000
    assert result["military_levy"] == 1000
    assert result["esv_fixed"] == 1902.34
    assert result["total_tax"] == 5902.34
    assert result["scheme"] == "3_percent_vat"
    assert result["vat_registered"] is True
    assert result["vat_included"] is False
    assert result["rules_year"] == 2026
    assert result["disclaimer"]


def test_api_middleware_requires_auth_and_rejects_non_admin(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    token = "test-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            missing = await client.get("/api/balance")
            forbidden = await client.post(
                "/api/admin/broadcast",
                headers={"X-Telegram-Init-Data": signed_init_data(token, 7)},
                json={"text": "test"},
            )
            return missing.status, forbidden.status

    assert run(exercise()) == (401, 403)


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


def test_transaction_counterparty_roundtrips_and_normalizes(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "get_exchange_rate", lambda currency: asyncio.sleep(0, result=1.0))

    created = run(bot.api_post_transaction(Request(body={
        "type": "expense", "amount": 500, "category": "Інше",
        "counterparty": "  Орендодавець  ",
    })))
    assert created.status == 201
    assert payload(created)["counterparty"] == "Орендодавець"   # trimmed

    plain = run(bot.api_post_transaction(Request(body={
        "type": "expense", "amount": 10, "category": "Інше",
    })))
    assert payload(plain)["counterparty"] is None               # absent -> null

    blank = run(bot.api_post_transaction(Request(body={
        "type": "expense", "amount": 10, "category": "Інше", "counterparty": "   ",
    })))
    assert payload(blank)["counterparty"] is None               # whitespace -> null

    listed = run(bot.api_get_transactions(Request()))
    parties = {r["counterparty"] for r in payload(listed)}
    assert "Орендодавець" in parties                            # persisted + returned by GET


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


def test_atomic_settings_updates_do_not_lose_parallel_changes(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)

    async def update_both():
        await asyncio.gather(
            bot.update_user_settings("user-1", lambda s: s["employees"].append("Alice")),
            bot.update_user_settings(
                "user-1", lambda s: s["time_categories"].update({"Focus": {"emoji": "F"}})),
        )
        return (
            await bot.user_settings_for("user-1"),
            await bot.user_settings_for("user-2"),
        )

    settings, other_settings = run(update_both())
    assert settings["employees"] == ["Alice"]
    assert "Focus" in settings["time_categories"]
    assert other_settings["employees"] == []
    assert "Focus" not in other_settings["time_categories"]


def test_owner_scoped_delete_cannot_remove_another_users_transaction(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def exercise():
        tx_id = await database.add_transaction(
            "owner", 10, "UAH", 10, "expense", "Інше", "",
            "2026-07-10", "2026-07-10 10:00:00",
        )
        deleted = await database.delete_transaction(tx_id, user_id="attacker")
        remaining = await database.get_transactions("owner")
        return deleted, remaining

    deleted, remaining = run(exercise())
    assert not deleted
    assert len(remaining) == 1
