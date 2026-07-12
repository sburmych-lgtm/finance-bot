"""Contract tests for irreversible, owner-scoped account deletion.

The confirmation phrase is intentionally explicit and case-sensitive.  The
handler may trim surrounding whitespace, but it must never accept a user id
from the request body: the authenticated Telegram user is the only owner that
may be deleted.
"""

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot


CONFIRMATION = "ВИДАЛИТИ"
USER_OWNED_TABLES = {
    "transactions",
    "time_tracks",
    "budgets",
    "recurring_operations",
    "notification_preferences",
    "notification_deliveries",
    "subscriptions",
    "users",
    "user_settings",
    "broadcast_receipts",
    "feature_reactions",
    "feature_comments",
}


def run(coro):
    return asyncio.run(coro)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "account-deletion.db"))
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


def seed_two_users(database):
    """Populate every current table that stores a Telegram user id."""
    conn = database.conn
    broadcast_id = conn.execute(
        "INSERT INTO broadcasts (text, created_at) VALUES (?, ?)",
        ("service announcement", "2026-07-12 10:00:00"),
    ).lastrowid

    for index, user_id in enumerate(("delete-me", "keep-me"), start=1):
        conn.execute(
            """INSERT INTO transactions
               (user_id, amount, currency, amount_uah, type, category,
                subcategory, description, date, timestamp)
               VALUES (?, ?, 'UAH', ?, 'expense', 'Інше', NULL, ?, ?, ?)""",
            (
                user_id,
                100 + index,
                100 + index,
                f"transaction-{user_id}",
                "2026-07-12",
                f"2026-07-12 10:00:0{index}",
            ),
        )
        conn.execute(
            """INSERT INTO time_tracks
               (user_id, minutes, category, description, date, timestamp)
               VALUES (?, ?, 'Інше', ?, ?, ?)""",
            (
                user_id,
                30 + index,
                f"time-{user_id}",
                "2026-07-12",
                f"2026-07-12 11:00:0{index}",
            ),
        )
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan) VALUES (?, 'vip')",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO budgets
               (user_id, type, category, monthly_limit_uah)
               VALUES (?, 'expense', 'Інше', ?)""",
            (user_id, 1000 + index),
        )
        conn.execute(
            """INSERT INTO recurring_operations
               (user_id, type, amount, currency, amount_uah, category,
                description, frequency, interval, start_date, anchor_day,
                next_due_date)
               VALUES (?, 'expense', ?, 'UAH', ?, 'Інше', ?, 'monthly', 1,
                       '2026-07-12', 12, '2026-07-12')""",
            (user_id, 200 + index, 200 + index, f"recurring-{user_id}"),
        )
        conn.execute(
            """INSERT INTO notification_preferences
               (user_id, weekly_digest_enabled) VALUES (?, 1)""",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO notification_deliveries
               (user_id, kind, period_key, status)
               VALUES (?, 'weekly_digest', '2026-W28', 'sent')""",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO users
               (user_id, username, first_name, last_name, language_code)
               VALUES (?, ?, ?, 'User', 'uk')""",
            (user_id, user_id, user_id),
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, settings_json) VALUES (?, ?)",
            (user_id, json.dumps({"owner": user_id})),
        )
        conn.execute(
            """INSERT INTO broadcast_receipts
               (broadcast_id, user_id, status, message_id, reason, created_at)
               VALUES (?, ?, 'sent', ?, NULL, ?)""",
            (broadcast_id, user_id, 1000 + index, "2026-07-12 10:01:00"),
        )
        conn.execute(
            """INSERT INTO feature_reactions
               (user_id, feature, reaction, created_at)
               VALUES (?, 'import', 'up', ?)""",
            (user_id, "2026-07-12 10:02:00"),
        )
        conn.execute(
            """INSERT INTO feature_comments
               (user_id, comment, created_at)
               VALUES (?, ?, ?)""",
            (user_id, f"comment-{user_id}", "2026-07-12 10:03:00"),
        )

    conn.commit()
    return broadcast_id


def user_row_counts(database, user_id):
    return {
        table: database.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        for table in USER_OWNED_TABLES
    }


def test_user_owned_table_inventory_is_explicit(monkeypatch, tmp_path):
    """A future user_id table must be consciously added to deletion logic."""
    database = use_database(monkeypatch, tmp_path)
    tables = {
        row[0]
        for row in database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        if "user_id"
        in {
            column[1]
            for column in database.conn.execute(f"PRAGMA table_info({row[0]})")
        }
    }

    assert tables == USER_OWNED_TABLES


def test_database_deletes_every_owned_row_atomically_and_preserves_other_user(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    broadcast_id = seed_two_users(database)

    result = run(database.delete_user_account("delete-me"))

    assert all(count == 0 for count in user_row_counts(database, "delete-me").values())
    assert all(count == 1 for count in user_row_counts(database, "keep-me").values())
    assert database.conn.execute(
        "SELECT COUNT(*) FROM broadcasts WHERE id = ?", (broadcast_id,)
    ).fetchone()[0] == 1
    assert result["deleted_user_id"] == "delete-me"
    assert set(result["deleted_rows"]) == USER_OWNED_TABLES


def test_database_rolls_back_all_tables_if_one_delete_fails(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    seed_two_users(database)
    before = user_row_counts(database, "delete-me")
    database.conn.execute(
        """CREATE TRIGGER fail_account_delete
           BEFORE DELETE ON subscriptions
           WHEN OLD.user_id = 'delete-me'
           BEGIN
             SELECT RAISE(ABORT, 'forced delete failure');
           END"""
    )
    database.conn.commit()

    with pytest.raises(Exception, match="forced delete failure"):
        run(database.delete_user_account("delete-me"))

    assert user_row_counts(database, "delete-me") == before


def test_delete_account_endpoint_requires_auth_and_exact_confirmation(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    seed_two_users(database)
    token = "account-delete-test-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    auth = {"X-Telegram-Init-Data": signed_init_data(token, "delete-me")}

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            unauthenticated = await client.delete(
                "/api/account", json={"confirmation": CONFIRMATION}
            )
            invalid_statuses = []
            for body in (
                {},
                {"confirmation": ""},
                {"confirmation": "видалити"},
                {"confirmation": "DELETE"},
            ):
                response = await client.delete("/api/account", headers=auth, json=body)
                invalid_statuses.append(response.status)
            return unauthenticated.status, invalid_statuses

    unauthenticated, invalid_statuses = run(exercise())

    assert unauthenticated == 401
    assert invalid_statuses == [400, 400, 400, 400]
    assert all(count == 1 for count in user_row_counts(database, "delete-me").values())
    assert all(count == 1 for count in user_row_counts(database, "keep-me").values())


def test_delete_account_uses_authenticated_owner_and_ignores_spoofed_user_id(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    seed_two_users(database)
    token = "account-delete-test-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    auth = {"X-Telegram-Init-Data": signed_init_data(token, "delete-me")}

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            response = await client.delete(
                "/api/account",
                headers=auth,
                json={
                    "confirmation": CONFIRMATION,
                    "user_id": "keep-me",
                },
            )
            return response.status, await response.json()

    status, body = run(exercise())

    assert status == 200
    assert body["ok"] is True
    assert all(count == 0 for count in user_row_counts(database, "delete-me").values())
    assert all(count == 1 for count in user_row_counts(database, "keep-me").values())
