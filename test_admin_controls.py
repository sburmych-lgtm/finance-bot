"""Test-first contracts for dangerous administrator operations."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

import bot
from security_controls import TokenBucketLimiter


def run(coro):
    return asyncio.run(coro)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "admin-controls.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def fixed_limiter(capacity=1):
    return TokenBucketLimiter(
        capacity=capacity,
        refill_rate=1,
        clock=lambda: 0,
    )


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
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})


def command_update(admin_id, text):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_id),
        message=FakeMessage(text),
    )


class FakeTelegramResponse:
    status = 200

    def __init__(self, message_id):
        self.message_id = message_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return {"ok": True, "result": {"message_id": self.message_id}}


class FakeTelegramSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, json):
        self.calls.append({"url": url, "json": json})
        return FakeTelegramResponse(1000 + len(self.calls))


def test_api_broadcast_previews_by_default_and_requires_one_time_confirmation(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    database.conn.executemany(
        "INSERT INTO users (user_id, first_name) VALUES (?, ?)",
        (("1001", "Real"), ("9990001", "Synthetic")),
    )
    database.conn.commit()

    token = "admin-preview-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter(capacity=20))
    monkeypatch.setattr(bot, "_broadcast_limiter", fixed_limiter(capacity=20))
    telegram_calls = []
    monkeypatch.setattr(
        bot.aiohttp,
        "ClientSession",
        lambda *_args, **_kwargs: FakeTelegramSession(telegram_calls),
    )
    headers = {
        "X-Telegram-Init-Data": signed_init_data(token, 42),
    }
    text = "Важливе оновлення Ruby Finance"

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            preview_response = await client.post(
                "/api/admin/broadcast", headers=headers, json={"text": text}
            )
            preview = await preview_response.json()
            broadcasts_after_preview = database.conn.execute(
                "SELECT COUNT(*) FROM broadcasts"
            ).fetchone()[0]

            assert preview_response.status == 200
            assert telegram_calls == []
            assert broadcasts_after_preview == 0
            assert preview["preview"] is True
            assert isinstance(preview["confirmation_token"], str)
            assert len(preview["confirmation_token"]) >= 16

            wrong_response = await client.post(
                "/api/admin/broadcast",
                headers=headers,
                json={
                    "text": text,
                    "confirm": True,
                    "confirmation_token": "wrong-token",
                },
            )
            assert wrong_response.status == 400
            assert telegram_calls == []

            tampered_response = await client.post(
                "/api/admin/broadcast",
                headers=headers,
                json={
                    "text": text + "!",
                    "confirm": True,
                    "confirmation_token": preview["confirmation_token"],
                },
            )
            assert tampered_response.status == 400
            assert telegram_calls == []

            confirmed_response = await client.post(
                "/api/admin/broadcast",
                headers=headers,
                json={
                    "text": text,
                    "confirm": True,
                    "confirmation_token": preview["confirmation_token"],
                },
            )
            confirmed = await confirmed_response.json()
            assert confirmed_response.status == 200
            assert confirmed["confirmed"] is True
            assert {call["json"]["chat_id"] for call in telegram_calls} == {42, 1001}

            sent_count = len(telegram_calls)
            replay_response = await client.post(
                "/api/admin/broadcast",
                headers=headers,
                json={
                    "text": text,
                    "confirm": True,
                    "confirmation_token": preview["confirmation_token"],
                },
            )
            assert replay_response.status in {400, 409}
            assert len(telegram_calls) == sent_count

    run(exercise())


def test_api_broadcast_confirmation_survives_cooldown_rejection(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    database.conn.execute(
        "INSERT INTO users (user_id, first_name) VALUES ('9990001', 'Synthetic')"
    )
    database.conn.commit()
    token = "admin-cooldown-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter(capacity=20))
    blocked_limiter = fixed_limiter()
    blocked_limiter.check("42")
    monkeypatch.setattr(bot, "_broadcast_limiter", blocked_limiter)
    headers = {"X-Telegram-Init-Data": signed_init_data(token, 42)}
    text = "Повідомлення після cooldown"

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            preview_response = await client.post(
                "/api/admin/broadcast", headers=headers, json={"text": text}
            )
            preview = await preview_response.json()
            confirmation = {
                "text": text,
                "confirm": True,
                "confirmation_token": preview["confirmation_token"],
            }
            blocked = await client.post(
                "/api/admin/broadcast", headers=headers, json=confirmation
            )
            monkeypatch.setattr(bot, "_broadcast_limiter", fixed_limiter())
            retried = await client.post(
                "/api/admin/broadcast", headers=headers, json=confirmation
            )
            return blocked.status, retried.status

    assert run(exercise()) == (429, 200)


def test_cleanup_users_command_is_rate_limited(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter())
    database.conn.execute(
        "INSERT INTO users (user_id, first_name) VALUES ('9990001', 'First')"
    )
    database.conn.commit()
    first = command_update(42, "/cleanup_users")
    second = command_update(42, "/cleanup_users")

    async def exercise():
        await bot.admin_cleanup_users(first, SimpleNamespace())
        database.conn.execute(
            "INSERT INTO users (user_id, first_name) VALUES ('9990002', 'Second')"
        )
        database.conn.commit()
        await bot.admin_cleanup_users(second, SimpleNamespace())
        return None

    run(exercise())
    candidate_2 = database.conn.execute(
        "SELECT 1 FROM users WHERE user_id = '9990002'"
    ).fetchone()

    assert candidate_2 is not None
    assert any("Зачекайте" in reply["text"] for reply in second.message.replies)


def test_in_chat_broadcast_preview_handles_full_telegram_length(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    database.conn.execute(
        "INSERT INTO users (user_id, first_name) VALUES ('1001', 'Real')"
    )
    database.conn.commit()
    update = command_update(42, "/broadcast " + ("А" * 4096))

    run(bot.admin_broadcast(update, SimpleNamespace()))

    assert len(update.message.replies) >= 2
    assert all(len(reply["text"]) <= 4096 for reply in update.message.replies)
    assert "reply_markup" in update.message.replies[-1]["kwargs"]


def test_cleanup_users_command_is_audited(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter())
    database.conn.execute(
        "INSERT INTO users (user_id, first_name) VALUES ('9990001', 'First')"
    )
    database.conn.commit()

    async def exercise():
        await bot.admin_cleanup_users(
            command_update(42, "/cleanup_users"), SimpleNamespace()
        )
        return await database.list_admin_audit()

    events = run(exercise())
    cleanup_events = [event for event in events if event["action"] == "cleanup_users"]

    assert len(cleanup_events) == 1
    assert cleanup_events[0]["admin_id"] == "42"
    assert cleanup_events[0]["status"] == "ok"
    assert json.loads(cleanup_events[0]["metadata_json"])["removed_count"] == 1


def test_reset_user_settings_command_is_rate_limited(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter())
    database.conn.executemany(
        "INSERT INTO user_settings (user_id, settings_json) VALUES (?, '{}')",
        (("target-a",), ("target-b",)),
    )
    database.conn.commit()
    first = command_update(42, "/reset_user_settings target-a")
    second = command_update(42, "/reset_user_settings target-b")

    async def exercise():
        await bot.admin_reset_user_settings(first, SimpleNamespace())
        await bot.admin_reset_user_settings(second, SimpleNamespace())
        return (
            await database.get_user_settings("target-a"),
            await database.get_user_settings("target-b"),
        )

    target_a, target_b = run(exercise())

    assert target_a is None
    assert target_b == {}
    assert any("Зачекайте" in reply["text"] for reply in second.message.replies)


def test_reset_user_settings_command_is_audited(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter())
    database.conn.execute(
        "INSERT INTO user_settings (user_id, settings_json) VALUES ('target-a', '{}')"
    )
    database.conn.commit()

    async def exercise():
        await bot.admin_reset_user_settings(
            command_update(42, "/reset_user_settings target-a"), SimpleNamespace()
        )
        return await database.list_admin_audit()

    events = run(exercise())
    reset_events = [event for event in events if event["action"] == "reset_user_settings"]

    assert len(reset_events) == 1
    assert reset_events[0]["admin_id"] == "42"
    assert reset_events[0]["target"] == "target-a"
    assert reset_events[0]["status"] == "ok"


def test_admin_audit_endpoint_remains_admin_only(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    token = "audit-endpoint-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(bot, "_admin_limiter", fixed_limiter(capacity=10))
    run(database.log_admin_action("42", "test_event", target="test"))

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            unauthenticated = await client.get("/api/admin/audit")
            non_admin = await client.get(
                "/api/admin/audit",
                headers={
                    "X-Telegram-Init-Data": signed_init_data(token, 7),
                },
            )
            admin = await client.get(
                "/api/admin/audit",
                headers={
                    "X-Telegram-Init-Data": signed_init_data(token, 42),
                },
            )
            return (
                unauthenticated.status,
                non_admin.status,
                admin.status,
                await admin.json(),
            )

    unauthenticated, non_admin, admin, body = run(exercise())

    assert unauthenticated == 401
    assert non_admin == 403
    assert admin == 200
    assert [event["action"] for event in body["events"]] == ["test_event"]
