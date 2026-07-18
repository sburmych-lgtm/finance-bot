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


def test_new_user_gated_but_can_opt_into_trial(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    monkeypatch.setattr(bot, "TRIAL_DAYS", 7)
    # new user is gated (no auto-trial) but eligible to start one
    assert run(bot.has_access("newbie")) is False
    st = run(bot.subscription_status("newbie"))
    assert st["state"] == "new" and st["trial_eligible"] is True
    # opt in → trial starts, access granted
    started = run(bot.start_free_trial("newbie"))
    assert started["state"] == "trial" and started["days_left"] == 7
    assert run(bot.has_access("newbie")) is True
    # trial is one-time: not eligible again
    assert run(bot.start_free_trial("newbie")) is None


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


def test_status_check_never_writes(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True)
    # a bare status read reports 'new' but must NOT persist a row
    assert run(bot.subscription_status("reader"))["state"] == "new"
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


# ── API write-gate (POST /api/transactions) ─────────────────────
import hashlib
import hmac
import json
import time as _time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer


def _auth(token, uid):
    params = {"auth_date": str(int(_time.time())),
              "user": json.dumps({"id": uid}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(params)}


_TX = {"type": "expense", "amount": 10, "currency": "UAH",
       "category": "Інше", "payment_source": "cash"}


def test_api_write_gated_402_when_expired(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=True)
    run(database.set_subscription("gated", "trial", "2020-01-01 00:00:00"))  # expired

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post("/api/transactions",
                                     headers=_auth(token, "gated"), json=_TX)
            return resp.status, await resp.json()

    status, body = run(exercise())
    assert status == 402
    assert body["code"] == "PAYWALL"
    assert body["paywall"]["state"] == "expired"
    assert body["paywall"]["price"] == bot.SUBSCRIPTION_PRICE_UAH


def test_api_write_free_when_paywall_off(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=False)
    run(database.set_subscription("gated", "trial", "2020-01-01 00:00:00"))  # expired

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post("/api/transactions",
                                     headers=_auth(token, "gated"), json=_TX)
            return resp.status

    assert run(exercise()) in (200, 201)  # dark → normal write


def test_api_write_free_for_vip(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=True, vip={"vipper"})

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post("/api/transactions",
                                     headers=_auth(token, "vipper"), json=_TX)
            return resp.status

    assert run(exercise()) in (200, 201)  # VIP bypass


def test_payment_claim_notifies_then_activation_grants_write(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=True, admins={"admin1"})
    monkeypatch.setattr(bot, "TRIAL_DAYS", 0)  # no trial → buyer starts gated
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((str(chat_id), reply_markup))
        return True

    monkeypatch.setattr(bot, "_tg_http_send", fake_send)
    bot._payment_claim_at.clear()

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            blocked = await client.post("/api/transactions",
                                        headers=_auth(token, "buyer"), json=_TX)
            claim = await client.post("/api/payment/claim", headers=_auth(token, "buyer"))
            claim_body = await claim.json()
            await bot._activate_paid_subscription("buyer")  # admin confirms
            allowed = await client.post("/api/transactions",
                                        headers=_auth(token, "buyer"), json=_TX)
            return blocked.status, claim.status, claim_body, allowed.status

    blocked, claim_status, body, allowed = run(exercise())
    assert blocked == 402                      # gated before paying
    assert claim_status == 200 and body["ok"] is True
    assert body["admins_notified"] == 1        # the one admin was pinged
    assert len(sent) == 1 and sent[0][0] == "admin1"
    assert allowed in (200, 201)               # activation restored write access


def test_first_payment_claim_not_throttled_on_fresh_monotonic_clock(monkeypatch, tmp_path):
    # Regression: right after a container boot/redeploy time.monotonic() is a
    # small number, so a first-time claimant (no prior timestamp, default 0)
    # must NOT be mistaken for a repeat inside the 60s throttle window.
    use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=True, admins={"admin1"})
    monkeypatch.setattr(bot, "TRIAL_DAYS", 0)
    monkeypatch.setattr("time.monotonic", lambda: 5.0)  # fresh clock, well under 60
    monkeypatch.setattr(bot, "_tg_http_send", _always_send)
    bot._payment_claim_at.clear()

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            claim = await client.post("/api/payment/claim", headers=_auth(token, "buyer"))
            return claim.status, await claim.json()

    status, body = run(exercise())
    assert status == 200
    assert body.get("throttled") is not True    # first claim must go through
    assert body["admins_notified"] == 1


async def _always_send(chat_id, text, reply_markup=None):
    return True


def test_plural_days_ukrainian_forms():
    assert bot._plural_days(1) == 'день'
    assert bot._plural_days(2) == 'дні'
    assert bot._plural_days(5) == 'днів'
    assert bot._plural_days(11) == 'днів'   # 11-19 always «днів»
    assert bot._plural_days(14) == 'днів'
    assert bot._plural_days(21) == 'день'   # «21 день», not «21 днів»
    assert bot._plural_days(22) == 'дні'
    assert bot._plural_days(25) == 'днів'


def test_paywall_text_declines_trial_length_correctly(monkeypatch):
    monkeypatch.setattr(bot, 'TRIAL_DAYS', 21)
    text = bot._paywall_bot_text({'trial_eligible': True})
    assert '21 день' in text
    assert '21 днів' not in text


def test_admin_paywalled_when_admin_is_vip_off_but_keeps_admin(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True, admins={"boss"})
    monkeypatch.setattr(bot, "TRIAL_DAYS", 0)
    monkeypatch.setattr(bot, "ADMIN_IS_VIP", False)
    # admin no longer VIP → hits the paywall (no record yet → 'new')
    assert run(bot.has_access("boss")) is False
    assert run(bot.subscription_status("boss"))["state"] == "new"
    # but keeps admin powers (can confirm payments)
    assert bot.is_admin("boss") is True
    # flip back → admin is VIP again
    monkeypatch.setattr(bot, "ADMIN_IS_VIP", True)
    assert run(bot.has_access("boss")) is True
    assert run(bot.subscription_status("boss"))["state"] == "vip"


def test_admin_subscription_reset_expire_and_guard(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    token = "gate-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    flags(monkeypatch, paywall=True, admins={"boss"})
    monkeypatch.setattr(bot, "TRIAL_DAYS", 5)
    monkeypatch.setattr(bot, "ADMIN_IS_VIP", False)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            await database.set_subscription("boss", "active", future(30))
            reset = await client.post("/api/admin/subscription", headers=_auth(token, "boss"),
                                      json={"user_id": "boss", "action": "reset"})
            reset_body = await reset.json()
            exp = await client.post("/api/admin/subscription", headers=_auth(token, "boss"),
                                    json={"user_id": "boss", "action": "expire"})
            exp_body = await exp.json()
            forbid = await client.post("/api/admin/subscription", headers=_auth(token, "rando"),
                                       json={"user_id": "boss", "action": "reset"})
            return reset.status, reset_body, exp_body, forbid.status

    status, reset_body, exp_body, forbid = run(exercise())
    assert status == 200
    assert reset_body["state"] == "new"       # reset → fresh, trial-eligible again
    assert exp_body["state"] == "expired"     # expire → paywall (trial used)
    assert forbid == 403


def test_subscription_reminders_sends_and_dedups(monkeypatch, tmp_path):
    database = use_db(monkeypatch, tmp_path)
    flags(monkeypatch, paywall=True, vip={"vipuser"})
    monkeypatch.setattr(bot, "PAYMENT_JAR_URL", "https://send.monobank.ua/jar/x")
    now = bot._sub_now()

    def at(delta):
        return (now + delta).strftime("%Y-%m-%d %H:%M:%S")

    flags(monkeypatch, paywall=True, vip={"5554"})  # real ids are numeric
    run(database.set_subscription("5551", "trial", at(timedelta(hours=12))))   # ending soon
    run(database.set_subscription("5552", "trial", at(timedelta(hours=-3))))   # just expired
    run(database.set_subscription("5553", "trial", at(timedelta(days=5))))     # plenty of time
    run(database.set_subscription("5554", "trial", at(timedelta(hours=6))))    # VIP → skip

    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(str(chat_id))
            return type("M", (), {"message_id": 7})()

    ctx = type("Ctx", (), {"bot": FakeBot()})()

    res = run(bot.subscription_reminders_job(ctx, now=now))
    ids = set(sent)
    assert "5551" in ids and "5552" in ids
    assert "5553" not in ids and "5554" not in ids
    assert res["sent"] == 2

    sent.clear()  # dedup: nothing re-sends
    run(bot.subscription_reminders_job(ctx, now=now))
    assert sent == []
