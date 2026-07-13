"""Contract tests for the CSV import API (Block 4, Stage 3).

The import flow is a three-step, owner-scoped pipeline:

    preview  -> parse CSV, flag likely duplicates, write nothing
    confirm  -> create one import batch, insert every valid row atomically
    delete   -> roll the whole batch back (transactions + batch row)

Every endpoint is scoped to the authenticated Telegram user: one user may
never see, confirm into, or roll back another user's data.
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


TOKEN = "import-api-test-token"


def run(coro):
    return asyncio.run(coro)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "import-api.db"))
    monkeypatch.setattr(bot, "db", database)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    return database


def auth_headers(user_id):
    params = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(params)}


PREVIEW_CSV = (
    "Дата,Сума,Валюта,Опис\n"
    "13.07.2026,-250.50,UAH,Кава\n"
    "12.07.2026,+30000,UAH,Зарплата\n"
)


def test_preview_parses_flags_duplicates_and_writes_nothing(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    headers = auth_headers("owner-1")

    # An existing transaction that one preview row will duplicate.
    run(database.add_transaction(
        "owner-1", 250.5, "UAH", 250.5, "expense", "Інше", "Кава",
        "2026-07-13", "2026-07-13 12:00:00",
    ))

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post(
                "/api/import/preview", headers=headers, json={"csv": PREVIEW_CSV}
            )
            return resp.status, await resp.json()

    status, body = run(exercise())

    assert status == 200
    assert body["summary"]["total"] == 2
    assert body["summary"]["income"] == 1
    assert body["summary"]["expense"] == 1
    rows = {r["description"]: r for r in body["rows"]}
    assert rows["Кава"]["duplicate"] is True       # matches the seeded row
    assert rows["Зарплата"]["duplicate"] is False
    # preview must never write: only the single seeded transaction exists.
    assert database.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id='owner-1'"
    ).fetchone()[0] == 1
    assert database.conn.execute(
        "SELECT COUNT(*) FROM import_batches"
    ).fetchone()[0] == 0


def test_confirm_creates_batch_and_tags_every_transaction(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    headers = auth_headers("owner-2")
    rows = [
        {"date": "2026-07-13", "type": "expense", "amount": 250.5,
         "currency": "UAH", "description": "Кава"},
        {"date": "2026-07-12", "type": "income", "amount": 30000.0,
         "currency": "UAH", "description": "Зарплата"},
    ]

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post(
                "/api/import/confirm", headers=headers,
                json={"rows": rows, "source": "privatbank.csv"},
            )
            return resp.status, await resp.json()

    status, body = run(exercise())

    assert status == 200
    assert body["ok"] is True
    assert body["imported"] == 2
    assert body["skipped"] == 0
    batch_id = body["batch_id"]
    # Both transactions carry the batch id; the batch records the row count.
    assert database.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id='owner-2' AND import_batch_id=?",
        (batch_id,),
    ).fetchone()[0] == 2
    assert database.conn.execute(
        "SELECT row_count FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()[0] == 2


def test_confirm_skips_invalid_rows_but_imports_the_rest(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    headers = auth_headers("owner-3")
    rows = [
        {"date": "2026-07-13", "type": "expense", "amount": 100.0,
         "currency": "UAH", "description": "ok"},
        {"date": "2026-07-13", "type": "expense", "amount": -5.0,   # bad amount
         "currency": "UAH", "description": "negative"},
        {"date": "2026-07-13", "type": "expense", "amount": 50.0,
         "currency": "UAH", "description": "unknown-cat", "category": "НемаєТакої"},
    ]

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post(
                "/api/import/confirm", headers=headers, json={"rows": rows}
            )
            return resp.status, await resp.json()

    status, body = run(exercise())

    assert status == 200
    assert body["imported"] == 1
    assert body["skipped"] == 2
    assert len(body["errors"]) == 2


def test_confirm_rejects_empty_rows_without_creating_a_batch(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    headers = auth_headers("owner-4")

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            resp = await client.post(
                "/api/import/confirm", headers=headers, json={"rows": []}
            )
            return resp.status

    assert run(exercise()) == 400
    assert database.conn.execute(
        "SELECT COUNT(*) FROM import_batches"
    ).fetchone()[0] == 0


def test_list_then_rollback_deletes_the_whole_batch(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    headers = auth_headers("owner-5")
    rows = [
        {"date": "2026-07-13", "type": "expense", "amount": 100.0,
         "currency": "UAH", "description": "a"},
        {"date": "2026-07-13", "type": "expense", "amount": 200.0,
         "currency": "UAH", "description": "b"},
    ]

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            confirm = await client.post(
                "/api/import/confirm", headers=headers, json={"rows": rows}
            )
            batch_id = (await confirm.json())["batch_id"]
            listing = await client.get("/api/import/batches", headers=headers)
            listed = await listing.json()
            deleted = await client.delete(
                f"/api/import/batches/{batch_id}", headers=headers
            )
            return batch_id, listed, deleted.status, await deleted.json()

    batch_id, listed, del_status, del_body = run(exercise())

    assert any(b["id"] == batch_id for b in listed["batches"])
    assert del_status == 200
    assert del_body["deleted_transactions"] == 2
    assert database.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE import_batch_id=?", (batch_id,)
    ).fetchone()[0] == 0
    assert database.conn.execute(
        "SELECT COUNT(*) FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()[0] == 0


def test_rollback_is_owner_scoped(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    owner = auth_headers("owner-6")
    attacker = auth_headers("attacker-6")
    rows = [{"date": "2026-07-13", "type": "expense", "amount": 100.0,
             "currency": "UAH", "description": "mine"}]

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            confirm = await client.post(
                "/api/import/confirm", headers=owner, json={"rows": rows}
            )
            batch_id = (await confirm.json())["batch_id"]
            # Attacker tries to roll back a batch that is not theirs.
            attack = await client.delete(
                f"/api/import/batches/{batch_id}", headers=attacker
            )
            return batch_id, attack.status

    batch_id, attack_status = run(exercise())

    # Attacker gets a 404 and the owner's data is untouched.
    assert attack_status == 404
    assert database.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE import_batch_id=?", (batch_id,)
    ).fetchone()[0] == 1


def test_import_endpoints_require_auth(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            preview = await client.post("/api/import/preview", json={"csv": "x"})
            confirm = await client.post("/api/import/confirm", json={"rows": []})
            listing = await client.get("/api/import/batches")
            deleted = await client.delete("/api/import/batches/1")
            return preview.status, confirm.status, listing.status, deleted.status

    statuses = run(exercise())
    assert statuses == (401, 401, 401, 401)
