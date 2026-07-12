import asyncio
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

import bot
from security_controls import TokenBucketLimiter


def run(coro):
    return asyncio.run(coro)


def signed_init_data(token, user_id, *, auth_date=None):
    params = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def test_init_data_uses_configurable_freshness_without_one_time_replay(monkeypatch):
    token = "freshness-test"
    now = int(time.time())
    raw = signed_init_data(token, 7, auth_date=now - 120)

    monkeypatch.setenv("INIT_DATA_MAX_AGE_SECONDS", "60")
    assert bot.validate_init_data(raw, token) is None

    monkeypatch.setenv("INIT_DATA_MAX_AGE_SECONDS", "300")
    first = bot.validate_init_data(raw, token)
    second = bot.validate_init_data(raw, token)
    assert first and second
    assert first["user"]["id"] == second["user"]["id"] == 7


def test_api_rejects_query_string_init_data(monkeypatch, tmp_path):
    token = "header-only-test"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "db", bot.Database(str(tmp_path / "header-only.db")))
    raw = signed_init_data(token, 8)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            response = await client.get("/api/me", params={"initData": raw})
            return response.status

    assert run(exercise()) == 401


def test_authenticated_write_rate_limit_returns_retry_after(monkeypatch, tmp_path):
    token = "rate-test"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "db", bot.Database(str(tmp_path / "rate.db")))
    monkeypatch.setattr(
        bot,
        "_write_limiter",
        TokenBucketLimiter(capacity=1, refill_rate=0.01),
    )
    raw = signed_init_data(token, 9)
    headers = {"X-Telegram-Init-Data": raw}

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            first = await client.delete("/api/settings", headers=headers)
            second = await client.delete("/api/settings", headers=headers)
            return first.status, second.status, second.headers.get("Retry-After")

    first, second, retry_after = run(exercise())
    assert first == 200
    assert second == 429
    assert int(retry_after) > 0


def test_daily_backup_uses_verified_snapshot_and_remote_readback(monkeypatch, tmp_path):
    source = tmp_path / "finance.db"
    database = bot.Database(str(source))
    monkeypatch.setattr(bot, "db", database)
    monkeypatch.setattr(bot, "DB_FILE", str(source))
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})

    remote = SimpleNamespace(bucket="bucket", key="daily/snapshot.db")
    calls = []
    monkeypatch.setattr(
        bot.S3BackupConfig,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(bucket="bucket")),
    )

    def fake_upload(artifact, config):
        calls.append((artifact, config))
        return remote

    monkeypatch.setattr(bot, "upload_and_verify_snapshot", fake_upload)
    monkeypatch.setattr(bot, "prune_remote_backups", lambda *_args, **_kwargs: [])

    class FakeBot:
        async def send_message(self, **_kwargs):
            raise AssertionError("successful remote backup must not alert")

        async def send_document(self, **_kwargs):
            raise AssertionError("remote backup must not use Telegram fallback")

    run(bot.daily_backup_job(SimpleNamespace(bot=FakeBot())))

    assert len(calls) == 1
    assert calls[0][0].path.is_file()
    assert bot.backup_status["last_remote_key"] == "daily/snapshot.db"
    assert bot.backup_status["last_error"] is None


def _prepare_retention_job(monkeypatch, tmp_path, *, upload_error=None):
    source = tmp_path / "finance.db"
    source.write_bytes(b"source-exists")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stale_one = backup_dir / "finance-20260709T030000Z.db"
    stale_two = backup_dir / "finance-20260710T030000Z.db"
    current = backup_dir / "finance-20260712T030000Z.db"
    for index, path in enumerate((stale_one, stale_two, current), start=1):
        path.write_bytes(f"snapshot-{index}".encode())
        os.utime(path, (index, index))

    artifact = SimpleNamespace(path=current, sha256="verified-checksum")
    monkeypatch.setattr(bot, "DB_FILE", str(source))
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "ADMIN_IDS", {"42"})
    monkeypatch.setattr(
        bot,
        "backup_status",
        {
            "last_success": None,
            "last_error": None,
            "last_remote_key": None,
            "last_checksum": None,
        },
    )
    monkeypatch.setenv("BACKUP_LOCAL_RETENTION", "1")
    monkeypatch.setattr(bot, "create_sqlite_snapshot", lambda *_args, **_kwargs: artifact)
    monkeypatch.setattr(
        bot.S3BackupConfig,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(bucket="bucket")),
    )

    def upload(_artifact, _config):
        if upload_error is not None:
            raise upload_error
        return SimpleNamespace(bucket="bucket", key="daily/current.db")

    monkeypatch.setattr(bot, "upload_and_verify_snapshot", upload)
    monkeypatch.setattr(bot, "prune_remote_backups", lambda *_args, **_kwargs: [])

    class FakeBot:
        async def send_message(self, **_kwargs):
            return None

        async def send_document(self, **_kwargs):
            raise AssertionError("configured remote backup must not use Telegram")

    return backup_dir, current, SimpleNamespace(bot=FakeBot())


def test_backup_retention_cleanup_runs_after_success(monkeypatch, tmp_path):
    backup_dir, current, context = _prepare_retention_job(monkeypatch, tmp_path)

    run(bot.daily_backup_job(context))

    assert list(backup_dir.glob("finance-*.db")) == [current]


def test_backup_retention_cleanup_also_runs_after_remote_failure(monkeypatch, tmp_path):
    backup_dir, current, context = _prepare_retention_job(
        monkeypatch,
        tmp_path,
        upload_error=RuntimeError("remote unavailable"),
    )

    run(bot.daily_backup_job(context))

    assert list(backup_dir.glob("finance-*.db")) == [current]
    assert bot.backup_status["last_error"] == "RuntimeError"


def test_configured_s3_backup_runs_without_admin_chat(monkeypatch, tmp_path):
    source = tmp_path / "finance.db"
    source.write_bytes(b"source-exists")
    artifact_path = tmp_path / "backups" / "finance-current.db"
    artifact_path.parent.mkdir()
    artifact_path.write_bytes(b"snapshot")
    artifact = SimpleNamespace(path=artifact_path, sha256="checksum")
    uploads = []

    monkeypatch.setattr(bot, "DB_FILE", str(source))
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "ADMIN_IDS", set())
    monkeypatch.setattr(bot, "create_sqlite_snapshot", lambda *_args, **_kwargs: artifact)
    monkeypatch.setattr(
        bot.S3BackupConfig,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(bucket="bucket")),
    )
    monkeypatch.setattr(
        bot,
        "upload_and_verify_snapshot",
        lambda value, config: uploads.append((value, config))
        or SimpleNamespace(bucket="bucket", key="daily/current.db"),
    )
    monkeypatch.setattr(bot, "prune_remote_backups", lambda *_args, **_kwargs: [])

    class NoTelegramBot:
        async def send_message(self, **_kwargs):
            raise AssertionError("S3 backup without admins must not need Telegram")

        async def send_document(self, **_kwargs):
            raise AssertionError("S3 backup without admins must not need Telegram")

    run(bot.daily_backup_job(SimpleNamespace(bot=NoTelegramBot())))

    assert len(uploads) == 1


def test_rate_limited_authenticated_request_has_no_database_side_effect(
    monkeypatch, tmp_path
):
    token = "rate-side-effect-test"
    database = bot.Database(str(tmp_path / "rate-side-effect.db"))
    upserted = []
    original_upsert = database.upsert_user

    async def tracking_upsert(user):
        upserted.append(str(user.id))
        await original_upsert(user)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(bot, "db", database)
    monkeypatch.setattr(database, "upsert_user", tracking_upsert)
    monkeypatch.setattr(
        bot,
        "_read_limiter",
        TokenBucketLimiter(capacity=1, refill_rate=0.01),
    )
    headers = {"X-Telegram-Init-Data": signed_init_data(token, 10)}

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            first = await client.get("/api/me", headers=headers)
            second = await client.get("/api/me", headers=headers)
            return first.status, second.status

    assert run(exercise()) == (200, 429)
    assert upserted == ["10"]


def test_expired_init_data_401_has_machine_readable_code(monkeypatch, tmp_path):
    token = "expired-init-data-test"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("INIT_DATA_MAX_AGE_SECONDS", "60")
    monkeypatch.setattr(bot, "db", bot.Database(str(tmp_path / "expired.db")))
    headers = {
        "X-Telegram-Init-Data": signed_init_data(
            token,
            11,
            auth_date=int(time.time()) - 120,
        )
    }

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            response = await client.get("/api/me", headers=headers)
            return response.status, await response.json()

    status, body = run(exercise())

    assert status == 401
    assert body["code"] == "INIT_DATA_EXPIRED"


def test_old_auth_date_with_bad_signature_is_invalid_not_expired(monkeypatch, tmp_path):
    token = "tampered-expiry-test"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("INIT_DATA_MAX_AGE_SECONDS", "60")
    monkeypatch.setattr(bot, "db", bot.Database(str(tmp_path / "tampered.db")))
    raw = signed_init_data(token, 12, auth_date=int(time.time()) - 120)
    tampered = raw.replace("hash=", "hash=00", 1)

    async def exercise():
        async with TestClient(TestServer(bot.build_api_app())) as client:
            response = await client.get(
                "/api/me", headers={"X-Telegram-Init-Data": tampered}
            )
            return response.status, await response.json()

    status, body = run(exercise())
    assert status == 401
    assert body["code"] == "INVALID_INIT_DATA"


def test_non_positive_init_data_window_uses_safe_default(monkeypatch):
    token = "safe-window-test"
    raw = signed_init_data(token, 13, auth_date=int(time.time()) - 120)

    for value in ("0", "-1", "invalid"):
        monkeypatch.setenv("INIT_DATA_MAX_AGE_SECONDS", value)
        assert bot.validate_init_data(raw, token) is not None
