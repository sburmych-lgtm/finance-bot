import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backup_service import (
    BackupConfigurationError,
    BackupIntegrityError,
    S3BackupConfig,
    create_sqlite_snapshot,
    prune_remote_backups,
    upload_and_verify_snapshot,
    verify_sqlite_file,
)


class FakeS3Client:
    def __init__(self, *, corrupt_download=False):
        self.objects = {}
        self.extra_args = None
        self.corrupt_download = corrupt_download
        self.deleted = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        with open(filename, "rb") as source:
            self.objects[(bucket, key)] = source.read()
        self.extra_args = ExtraArgs

    def download_file(self, bucket, key, filename):
        payload = self.objects[(bucket, key)]
        if self.corrupt_download:
            payload = payload[:-1] + bytes([payload[-1] ^ 0xFF])
        with open(filename, "wb") as destination:
            destination.write(payload)

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": list(getattr(self, "listed_objects", [])),
            "IsTruncated": False,
        }


def _create_wal_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, amount REAL)")
    connection.execute("INSERT INTO transactions(amount) VALUES (10), (20)")
    connection.commit()
    return connection


def test_create_sqlite_snapshot_copies_wal_state_and_records_checksum(tmp_path):
    source_path = tmp_path / "finance.db"
    source_connection = _create_wal_database(source_path)

    artifact = create_sqlite_snapshot(
        source_path,
        tmp_path / "backups",
        now=datetime(2026, 7, 12, 20, 15, tzinfo=timezone.utc),
    )

    with sqlite3.connect(artifact.path) as snapshot:
        count = snapshot.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert count == 2
    assert artifact.path.name == "finance-20260712T201500Z.db"
    assert artifact.size_bytes == artifact.path.stat().st_size
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert verify_sqlite_file(artifact.path, expected_sha256=artifact.sha256) == artifact
    assert not list((tmp_path / "backups").glob("*.tmp"))

    source_connection.close()


def test_verify_sqlite_file_rejects_corruption(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")

    with pytest.raises(BackupIntegrityError):
        verify_sqlite_file(corrupt)


def test_upload_download_and_integrity_verification(tmp_path):
    source_path = tmp_path / "finance.db"
    connection = _create_wal_database(source_path)
    artifact = create_sqlite_snapshot(source_path, tmp_path / "backups")
    client = FakeS3Client()
    config = S3BackupConfig(
        bucket="ruby-backups",
        prefix="production/worker",
        endpoint_url="https://example.invalid",
        region_name="auto",
        server_side_encryption="AES256",
    )

    remote = upload_and_verify_snapshot(artifact, config, client=client)

    assert remote.bucket == "ruby-backups"
    assert remote.key == f"production/worker/{artifact.path.name}"
    assert remote.sha256 == artifact.sha256
    assert remote.size_bytes == artifact.size_bytes
    assert client.extra_args == {
        "ContentType": "application/vnd.sqlite3",
        "Metadata": {"sha256": artifact.sha256},
        "ServerSideEncryption": "AES256",
    }
    connection.close()


def test_corrupt_remote_readback_is_rejected_and_deleted(tmp_path):
    source_path = tmp_path / "finance.db"
    connection = _create_wal_database(source_path)
    artifact = create_sqlite_snapshot(source_path, tmp_path / "backups")
    client = FakeS3Client(corrupt_download=True)
    config = S3BackupConfig(bucket="ruby-backups", prefix="daily")

    with pytest.raises(BackupIntegrityError):
        upload_and_verify_snapshot(artifact, config, client=client)

    assert client.deleted == [("ruby-backups", f"daily/{artifact.path.name}")]
    connection.close()


def test_s3_config_from_env_is_optional_and_does_not_require_static_credentials():
    assert S3BackupConfig.from_env({}) is None

    config = S3BackupConfig.from_env({
        "BACKUP_S3_BUCKET": "ruby-backups",
        "BACKUP_S3_PREFIX": "/prod//daily/",
        "BACKUP_S3_ENDPOINT_URL": "https://r2.example.invalid",
        "BACKUP_S3_REGION": "auto",
        "BACKUP_S3_ACCESS_KEY_ID": "key-id",
        "BACKUP_S3_SECRET_ACCESS_KEY": "secret-key",
        "BACKUP_S3_SSE": "AES256",
    })

    assert config == S3BackupConfig(
        bucket="ruby-backups",
        prefix="prod/daily",
        endpoint_url="https://r2.example.invalid",
        region_name="auto",
        access_key_id="key-id",
        secret_access_key="secret-key",
        server_side_encryption="AES256",
    )

    with pytest.raises(BackupConfigurationError):
        S3BackupConfig(bucket="ruby-backups", prefix="")


def test_prune_remote_backups_deletes_only_expired_objects_under_prefix():
    now = datetime(2026, 7, 12, 20, 15, tzinfo=timezone.utc)
    client = FakeS3Client()
    client.listed_objects = [
        {"Key": "prod/finance-20260601T030000Z.db", "LastModified": now - timedelta(days=31)},
        {"Key": "prod/finance-20260701T030000Z.db", "LastModified": now - timedelta(days=29)},
        {"Key": "prod/customer-export.csv", "LastModified": now - timedelta(days=90)},
        {"Key": "other/finance-old.db", "LastModified": now - timedelta(days=90)},
    ]
    config = S3BackupConfig(bucket="ruby-backups", prefix="prod")

    deleted = prune_remote_backups(
        config,
        retention_days=30,
        client=client,
        now=now,
    )

    assert deleted == ["prod/finance-20260601T030000Z.db"]
    assert client.deleted == [("ruby-backups", "prod/finance-20260601T030000Z.db")]
