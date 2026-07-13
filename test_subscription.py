"""Contract tests for the monetization gate (Крок 5).

Ships dark: with PAYWALL_ENABLED off, everyone has access and no code path
changes for current users. With it on: VIP is free-forever, a brand-new user
gets a 5-day trial, a lapsed trial loses write-access (reads stay free), and a
paid subscription restores it.
"""
import asyncio
from datetime import datetime, timedelta

import bot


def run(coro):
    return asyncio.run(coro)


def use_db(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "subs.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def flags(monkeypatch, *, paywall, vip=(), admins=()):
    monkeypatch.setattr(bot, "PAYWALL_ENABLED", paywall)
    monkeypatch.setattr(bot, "VIP_IDS", set(vip))
    monkeypatch.setattr(bot, "ADMIN_IDS", set(admins))


def future(days):
    return (bot._sub_now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_paywall_off_everyone_has_access(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=False)
    assert run(bot.has_access("nobody")) is True


def test_vip_and_admin_free_forever(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True, vip={"7979208019"}, admins={"963610407"})
    assert run(bot.has_access("7979208019")) is True
    assert run(bot.has_access("963610407")) is True
    assert run(bot.subscription_status("7979208019"))["state"] == "vip"
    assert run(bot.subscription_status("963610407"))["state"] == "vip"


def test_new_user_gets_trial(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    # first gated action → access granted + trial persisted
    assert run(bot.has_access("newbie")) is True
    status = run(bot.subscription_status("newbie"))
    assert status["state"] == "trial"
    assert 0 < status["days_left"] <= 5


def test_lapsed_trial_loses_write_access(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    run(database.set_subscription("late", "trial", "2020-01-01 00:00:00"))
    assert run(bot.has_access("late")) is False
    assert run(bot.subscription_status("late"))["state"] == "expired"


def test_paid_subscription_restores_access(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    run(database.set_subscription("payer", "trial", "2020-01-01 00:00:00"))
    assert run(bot.has_access("payer")) is False
    run(database.set_subscription("payer", "active", future(30)))
    assert run(bot.has_access("payer")) is True
    assert run(bot.subscription_status("payer"))["state"] == "active"


def test_status_check_does_not_start_trial(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    # a bare status read reports trial but must NOT persist a row
    assert run(bot.subscription_status("reader"))["state"] == "trial"
    assert run(database.get_subscription("reader")) is None


def test_price_and_jar_surface_in_status(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    monkeypatch.setattr(bot, "SUBSCRIPTION_PRICE_UAH", 149)
    monkeypatch.setattr(bot, "PAYMENT_JAR_URL", "https://send.monobank.ua/jar/demo")
    status = run(bot.subscription_status("someone"))
    assert status["price"] == 149
    assert status["jar_url"] == "https://send.monobank.ua/jar/demo"
    assert status["paywall_enabled"] is True
