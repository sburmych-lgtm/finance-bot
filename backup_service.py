"""Consistent, verifiable SQLite backups with optional S3-compatible storage.

The module is deliberately synchronous. Callers running inside asyncio should
use ``asyncio.to_thread`` so a large snapshot or remote transfer cannot block
the bot event loop.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol


_CHUNK_SIZE = 1024 * 1024
_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9._-]+$")
_BACKUP_OBJECT_NAME = re.compile(
    r"^finance-\d{8}T\d{6}Z(?:-\d+)?\.db$"
)


class BackupError(RuntimeError):
    """Base class for backup failures."""


class BackupIntegrityError(BackupError):
    """The snapshot cannot be trusted or restored as SQLite."""


class BackupConfigurationError(BackupError):
    """Off-site backup configuration is incomplete or invalid."""


class BackupRemoteError(BackupError):
    """The remote object could not be uploaded and verified."""


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RemoteBackup:
    bucket: str
    key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class S3BackupConfig:
    """S3-compatible destination.

    Static credentials are optional so Railway/AWS workload credentials can
    be used. If one static credential is supplied, both are required.
    """

    bucket: str
    prefix: str = "ruby-finance"
    endpoint_url: str | None = None
    region_name: str | None = None
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    server_side_encryption: str | None = None

    def __post_init__(self) -> None:
        bucket = self.bucket.strip()
        if not bucket:
            raise BackupConfigurationError("BACKUP_S3_BUCKET must not be empty")

        prefix = "/".join(part for part in self.prefix.strip().split("/") if part)
        if not prefix:
            raise BackupConfigurationError("BACKUP_S3_PREFIX must be a dedicated non-empty prefix")
        if any(part in {".", ".."} for part in prefix.split("/") if part):
            raise BackupConfigurationError("BACKUP_S3_PREFIX contains an unsafe segment")
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise BackupConfigurationError(
                "BACKUP_S3_ACCESS_KEY_ID and BACKUP_S3_SECRET_ACCESS_KEY must be set together"
            )

        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "prefix", prefix)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> S3BackupConfig | None:
        values = os.environ if environ is None else environ
        bucket = (values.get("BACKUP_S3_BUCKET") or "").strip()
        related = {
            key: (values.get(key) or "").strip()
            for key in (
                "BACKUP_S3_PREFIX",
                "BACKUP_S3_ENDPOINT_URL",
                "BACKUP_S3_REGION",
                "BACKUP_S3_ACCESS_KEY_ID",
                "BACKUP_S3_SECRET_ACCESS_KEY",
                "BACKUP_S3_SESSION_TOKEN",
                "BACKUP_S3_SSE",
            )
        }
        if not bucket:
            if any(related.values()):
                raise BackupConfigurationError(
                    "BACKUP_S3_BUCKET is required when other BACKUP_S3_* values are set"
                )
            return None
        return cls(
            bucket=bucket,
            prefix=related["BACKUP_S3_PREFIX"] or "ruby-finance",
            endpoint_url=related["BACKUP_S3_ENDPOINT_URL"] or None,
            region_name=related["BACKUP_S3_REGION"] or None,
            access_key_id=related["BACKUP_S3_ACCESS_KEY_ID"] or None,
            secret_access_key=related["BACKUP_S3_SECRET_ACCESS_KEY"] or None,
            session_token=related["BACKUP_S3_SESSION_TOKEN"] or None,
            server_side_encryption=related["BACKUP_S3_SSE"] or None,
        )


class _S3Client(Protocol):
    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict | None = None) -> None: ...

    def download_file(self, bucket: str, key: str, filename: str) -> None: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...

    def list_objects_v2(self, **kwargs) -> dict: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sqlite_file(
    path: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
) -> BackupArtifact:
    """Verify file presence, checksum and SQLite structural integrity."""

    candidate = Path(path).resolve()
    if not candidate.is_file():
        raise BackupIntegrityError(f"backup file does not exist: {candidate}")
    size_bytes = candidate.stat().st_size
    if size_bytes == 0:
        raise BackupIntegrityError("backup file is empty")

    actual_sha256 = _sha256_file(candidate)
    if expected_sha256 and not hmac.compare_digest(
        actual_sha256.lower(), expected_sha256.strip().lower()
    ):
        raise BackupIntegrityError("backup SHA-256 does not match")

    try:
        with closing(sqlite3.connect(str(candidate), timeout=10)) as connection:
            connection.execute("PRAGMA query_only=ON")
            results = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError("backup is not a readable SQLite database") from exc

    if results != ["ok"]:
        detail = "; ".join(str(item) for item in results[:5]) or "no result"
        raise BackupIntegrityError(f"SQLite integrity_check failed: {detail}")

    return BackupArtifact(
        path=candidate,
        size_bytes=size_bytes,
        sha256=actual_sha256,
    )


def create_sqlite_snapshot(
    source_path: str | os.PathLike[str],
    destination_dir: str | os.PathLike[str],
    *,
    prefix: str = "finance",
    now: datetime | None = None,
) -> BackupArtifact:
    """Create an atomic, consistent SQLite snapshot, including live WAL state."""

    source = Path(source_path).resolve()
    if not source.is_file():
        raise BackupIntegrityError(f"source database does not exist: {source}")
    if not _SAFE_PREFIX.fullmatch(prefix):
        raise BackupConfigurationError("backup filename prefix contains unsafe characters")

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)

    destination = Path(destination_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    final_path = destination / f"{stem}.db"
    suffix = 1
    while final_path.exists():
        final_path = destination / f"{stem}-{suffix}.db"
        suffix += 1
    temp_path = destination / f".{final_path.name}.{uuid.uuid4().hex}.tmp"

    if source == final_path or source == temp_path:
        raise BackupConfigurationError("source and backup destination must differ")

    try:
        with closing(sqlite3.connect(str(source), timeout=30)) as source_connection:
            with closing(sqlite3.connect(str(temp_path), timeout=30)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.commit()

        verified_temp = verify_sqlite_file(temp_path)
        os.replace(temp_path, final_path)
        return BackupArtifact(
            path=final_path,
            size_bytes=verified_temp.size_bytes,
            sha256=verified_temp.sha256,
        )
    except BackupError:
        raise
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError("could not create a consistent SQLite snapshot") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _build_s3_client(config: S3BackupConfig):
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - production dependency guard
        raise BackupConfigurationError(
            "boto3 is required when S3-compatible backups are enabled"
        ) from exc

    kwargs = {}
    if config.endpoint_url:
        kwargs["endpoint_url"] = config.endpoint_url
    if config.region_name:
        kwargs["region_name"] = config.region_name
    if config.access_key_id:
        kwargs["aws_access_key_id"] = config.access_key_id
        kwargs["aws_secret_access_key"] = config.secret_access_key
    if config.session_token:
        kwargs["aws_session_token"] = config.session_token
    return boto3.client("s3", **kwargs)


def upload_and_verify_snapshot(
    artifact: BackupArtifact,
    config: S3BackupConfig,
    *,
    client: _S3Client | None = None,
) -> RemoteBackup:
    """Upload a snapshot, download it again, then verify checksum + SQLite."""

    local = verify_sqlite_file(artifact.path, expected_sha256=artifact.sha256)
    if local.size_bytes != artifact.size_bytes:
        raise BackupIntegrityError("backup size changed before upload")

    object_key = f"{config.prefix}/{local.path.name}" if config.prefix else local.path.name
    s3 = client or _build_s3_client(config)
    extra_args = {
        "ContentType": "application/vnd.sqlite3",
        "Metadata": {"sha256": local.sha256},
    }
    if config.server_side_encryption:
        extra_args["ServerSideEncryption"] = config.server_side_encryption

    uploaded = False

    def delete_unverified_object() -> None:
        if not uploaded:
            return
        try:
            s3.delete_object(Bucket=config.bucket, Key=object_key)
        except Exception:
            # Preserve the original verification/transport error. The caller
            # should alert an operator either way.
            pass

    try:
        s3.upload_file(str(local.path), config.bucket, object_key, ExtraArgs=extra_args)
        uploaded = True
        with tempfile.TemporaryDirectory(prefix="ruby-backup-verify-") as temp_dir:
            downloaded = Path(temp_dir) / local.path.name
            s3.download_file(config.bucket, object_key, str(downloaded))
            remote = verify_sqlite_file(downloaded, expected_sha256=local.sha256)
            if remote.size_bytes != local.size_bytes:
                raise BackupIntegrityError("remote backup size does not match local snapshot")
    except BackupIntegrityError:
        delete_unverified_object()
        raise
    except Exception as exc:
        delete_unverified_object()
        raise BackupRemoteError(
            f"could not upload and verify s3://{config.bucket}/{object_key}"
        ) from exc

    return RemoteBackup(
        bucket=config.bucket,
        key=object_key,
        size_bytes=local.size_bytes,
        sha256=local.sha256,
    )


def prune_remote_backups(
    config: S3BackupConfig,
    *,
    retention_days: int = 30,
    client: _S3Client | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Delete verified-backup objects older than the declared retention.

    Bucket lifecycle rules remain a useful second layer, but enforcing the
    same limit here keeps the privacy promise independent of provider setup.
    Only objects under the configured prefix are ever considered.
    """
    if retention_days <= 0:
        raise BackupConfigurationError("backup retention must be positive")

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = moment.astimezone(timezone.utc) - timedelta(days=retention_days)
    s3 = client or _build_s3_client(config)
    prefix = f"{config.prefix}/" if config.prefix else ""
    continuation_token: str | None = None
    deleted: list[str] = []

    while True:
        params = {"Bucket": config.bucket, "Prefix": prefix}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        try:
            page = s3.list_objects_v2(**params)
        except Exception as exc:
            raise BackupRemoteError("could not list remote backups for retention") from exc

        for item in page.get("Contents") or []:
            key = str(item.get("Key") or "")
            modified = item.get("LastModified")
            filename = key.rsplit("/", 1)[-1]
            if (
                not key.startswith(prefix)
                or not _BACKUP_OBJECT_NAME.fullmatch(filename)
                or not isinstance(modified, datetime)
            ):
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            if modified.astimezone(timezone.utc) >= cutoff:
                continue
            try:
                s3.delete_object(Bucket=config.bucket, Key=key)
            except Exception as exc:
                raise BackupRemoteError(f"could not delete expired backup {key}") from exc
            deleted.append(key)

        if not page.get("IsTruncated"):
            break
        continuation_token = page.get("NextContinuationToken")
        if not continuation_token:
            raise BackupRemoteError("remote backup listing was truncated without a token")

    return deleted
