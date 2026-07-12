import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

import bot


class Request(dict):
    def __init__(self, user_id="user-1", query=None, body=None):
        super().__init__(user_id=user_id, tg_user={"id": user_id})
        self.rel_url = SimpleNamespace(query=query or {})
        self._body = body

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


def payload(response):
    return json.loads(response.body)


def use_database(monkeypatch, tmp_path):
    database = bot.Database(str(tmp_path / "block1.db"))
    monkeypatch.setattr(bot, "db", database)
    return database


def transaction_body(**overrides):
    return {
        "type": "expense",
        "amount": 40,
        "currency": "UAH",
        "category": "Кафе",
        "subcategory": None,
        "description": "Ранкова кава",
        **overrides,
    }


def test_client_request_id_migration_adds_nullable_column_and_scoped_unique_index(
    tmp_path,
):
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
            subcategory TEXT,
            description TEXT,
            date DATE NOT NULL,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.execute(
        """CREATE TABLE time_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date DATE NOT NULL,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.commit()
    connection.close()

    database = bot.Database(str(path))
    columns = {
        row[1] for row in database.conn.execute("PRAGMA table_info(transactions)")
    }
    indexes = [
        row[0]
        for row in database.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='transactions'"
        )
        if row[0]
    ]

    assert "client_request_id" in columns
    assert any(
        "UNIQUE" in sql.upper()
        and "user_id" in sql
        and "client_request_id" in sql
        for sql in indexes
    )
    time_columns = {
        row[1] for row in database.conn.execute("PRAGMA table_info(time_tracks)")
    }
    time_indexes = [
        row[0]
        for row in database.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='time_tracks'"
        )
        if row[0]
    ]
    assert "client_request_id" in time_columns
    assert any(
        "UNIQUE" in sql.upper()
        and "user_id" in sql
        and "client_request_id" in sql
        for sql in time_indexes
    )


def test_repeated_transaction_post_is_idempotent_per_user(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot,
        "get_exchange_rate",
        lambda _currency: asyncio.sleep(0, result=1.0),
    )
    body = transaction_body(client_request_id="req-20260712-0001")

    first = payload(run(bot.api_post_transaction(Request(body=body))))
    repeated = payload(run(bot.api_post_transaction(Request(body=body))))
    other_user = payload(
        run(bot.api_post_transaction(Request(user_id="user-2", body=body)))
    )

    assert first["duplicate"] is False
    assert first["idempotent"] is True
    assert repeated["duplicate"] is True
    assert repeated["idempotent"] is True
    assert repeated["id"] == first["id"]
    assert other_user["duplicate"] is False
    assert other_user["id"] != first["id"]
    assert len(run(database.get_transactions("user-1"))) == 1
    assert len(run(database.get_transactions("user-2"))) == 1


def test_reused_client_request_id_with_different_payload_is_a_conflict(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot,
        "get_exchange_rate",
        lambda _currency: asyncio.sleep(0, result=1.0),
    )
    key = "req-conflict-0001"

    created = run(
        bot.api_post_transaction(
            Request(body=transaction_body(client_request_id=key))
        )
    )
    conflict = run(
        bot.api_post_transaction(
            Request(body=transaction_body(client_request_id=key, amount=41))
        )
    )

    assert created.status == 201
    assert conflict.status == 409
    assert payload(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(run(database.get_transactions("user-1"))) == 1


def test_transaction_replay_survives_later_category_deletion(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot,
        "get_exchange_rate",
        lambda _currency: asyncio.sleep(0, result=1.0),
    )
    body = transaction_body(client_request_id="req-deleted-category")
    created = run(bot.api_post_transaction(Request(body=body)))

    async def remove_category():
        settings = await bot.user_settings_for("user-1")
        settings["categories"]["expense"].pop("Кафе", None)
        await bot.save_user_settings("user-1", settings)

    run(remove_category())
    replay = run(bot.api_post_transaction(Request(body=body)))

    assert created.status == 201
    assert replay.status == 200
    assert payload(replay)["duplicate"] is True
    assert payload(replay)["id"] == payload(created)["id"]
    assert len(run(database.get_transactions("user-1"))) == 1


def test_concurrent_duplicate_posts_create_exactly_one_transaction(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot,
        "get_exchange_rate",
        lambda _currency: asyncio.sleep(0, result=1.0),
    )
    body = transaction_body(client_request_id="req-concurrent-0001")

    async def exercise():
        responses = await asyncio.gather(
            bot.api_post_transaction(Request(body=body)),
            bot.api_post_transaction(Request(body=body)),
        )
        return [payload(response) for response in responses]

    results = run(exercise())

    assert {result["duplicate"] for result in results} == {False, True}
    assert len({result["id"] for result in results}) == 1
    assert len(run(database.get_transactions("user-1"))) == 1


@pytest.mark.parametrize(
    "client_request_id",
    [
        "",
        " leading-space",
        "trailing-space ",
        "contains space",
        "line\nbreak",
        "x" * 129,
        123,
        [],
    ],
)
def test_transaction_rejects_invalid_client_request_id(
    monkeypatch,
    tmp_path,
    client_request_id,
):
    database = use_database(monkeypatch, tmp_path)
    response = run(
        bot.api_post_transaction(
            Request(body=transaction_body(client_request_id=client_request_id))
        )
    )

    assert response.status == 400
    assert "client_request_id" in payload(response)["detail"]
    assert run(database.get_transactions("user-1")) == []


def test_quick_templates_use_all_history_and_are_fully_user_isolated(
    monkeypatch,
    tmp_path,
):
    database = use_database(monkeypatch, tmp_path)

    async def seed():
        settings = await bot.user_settings_for("user-1")
        settings["categories"]["expense"]["Кафе"]["subcategories"] = ["Напої"]
        await bot.save_user_settings("user-1", settings)
        # The repeated template is older than the latest 15 rows. An endpoint
        # that accidentally aggregates Store/listTransactions(15) cannot find it.
        for index in range(16):
            await database.add_transaction(
                "user-1",
                40,
                "UAH",
                40,
                "expense",
                "Кафе",
                "Ранкова кава",
                "2026-06-01",
                f"2026-06-01 08:{index:02d}:00",
                subcategory="Напої",
            )
        for index in range(15):
            await database.add_transaction(
                "user-1",
                100 + index,
                "UAH",
                100 + index,
                "expense",
                "Транспорт",
                f"Поїздка {index}",
                "2026-07-12",
                f"2026-07-12 10:{index:02d}:00",
            )
        await database.add_transaction(
            "user-2",
            999999,
            "USD",
            999999,
            "income",
            "Інше",
            "SECRET-OTHER-USER",
            "2026-07-12",
            "2026-07-12 23:59:59",
        )

    run(seed())
    response = run(bot.api_quick_templates(Request(query={"limit": "3"})))
    result = payload(response)

    assert response.status == 200
    assert result["templates"][0] == {
        "amount": 40.0,
        "currency": "UAH",
        "type": "expense",
        "category": "Кафе",
        "subcategory": "Напої",
        "comment": "Ранкова кава",
        "payment_source": None,
        "usage_count": 16,
    }
    assert result["last_operation"] == {
        "amount": 114.0,
        "currency": "UAH",
        "type": "expense",
        "category": "Транспорт",
        "subcategory": None,
        "comment": "Поїздка 14",
        "payment_source": None,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "SECRET-OTHER-USER" not in serialized
    for item in [*result["templates"], result["last_operation"]]:
        assert not {"id", "user_id", "date", "timestamp", "client_request_id"} & item.keys()


def test_quick_templates_empty_state_and_limit_validation(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)

    empty = run(bot.api_quick_templates(Request(query={})))
    too_large = run(bot.api_quick_templates(Request(query={"limit": "11"})))
    invalid = run(bot.api_quick_templates(Request(query={"limit": "abc"})))

    assert payload(empty) == {"templates": [], "last_operation": None}
    assert too_large.status == 400
    assert invalid.status == 400


def test_quick_templates_filter_deleted_categories_defensively(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)

    async def seed():
        settings = await bot.user_settings_for("user-1")
        settings["categories"]["expense"].pop("Кафе", None)
        await bot.save_user_settings("user-1", settings)
        for index in range(8):
            await database.add_transaction(
                "user-1", 40, "UAH", 40, "expense", "Кафе", "stale",
                "2026-07-01", f"2026-07-01 08:{index:02d}:00",
            )
        await database.add_transaction(
            "user-1", 10, "UAH", 10, "expense", "Інше", "valid",
            "2026-07-12", "2026-07-12 12:00:00",
        )

    run(seed())
    result = payload(run(bot.api_quick_templates(Request(query={"limit": "3"}))))

    assert [item["category"] for item in result["templates"]] == ["Інше"]
    assert result["last_operation"]["category"] == "Інше"


def test_time_track_posts_are_idempotent_and_conflicts_are_rejected(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)

    async def seed_category():
        settings = await bot.user_settings_for("user-1")
        settings["time_categories"] = {"Робота": {"emoji": "⏱"}}
        await bot.save_user_settings("user-1", settings)

    run(seed_category())
    body = {
        "minutes": 45,
        "category": "Робота",
        "description": "Планування",
        "client_request_id": "time-20260712-0001",
    }

    first = run(bot.api_time_tracks_create(Request(body=body)))
    repeated = run(bot.api_time_tracks_create(Request(body=body)))
    conflict = run(bot.api_time_tracks_create(Request(body={**body, "minutes": 46})))

    assert first.status == 201
    assert repeated.status == 200
    assert payload(repeated)["duplicate"] is True
    assert payload(repeated)["id"] == payload(first)["id"]
    assert conflict.status == 409
    assert payload(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(run(database.get_time_tracks("user-1"))) == 1
