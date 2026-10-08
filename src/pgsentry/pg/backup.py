"""Backup freshness: how old is the 'last successful backup' marker?

The backup job is expected to touch a file (or an S3 object) when it succeeds. pgsentry
only reads that marker; it does not look at the backup itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pgsentry.config import BackupConfig, S3Marker
from pgsentry.formatting import human_duration
from pgsentry.models import CheckResult, Sample, Status, grade

S3ClientFactory = Callable[[S3Marker], Any]


class BackupMarkerError(Exception):
    pass


def _read_file_marker(path: Path) -> datetime:
    try:
        text = path.read_text(encoding="utf-8").strip()
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except FileNotFoundError as exc:
        raise BackupMarkerError(f"marker file {path} not found") from exc
    except OSError as exc:
        raise BackupMarkerError(f"cannot read marker file {path}: {exc.strerror}") from exc
    if not text:
        return mtime  # an empty file that is touched on success: use its mtime
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=UTC)  # epoch seconds
        except ValueError as exc:
            raise BackupMarkerError(
                f"marker file {path} holds neither an ISO-8601 time nor epoch seconds"
            ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _default_s3_client(marker: S3Marker) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise BackupMarkerError("S3 marker needs boto3: pip install 'pgsentry[s3]'") from exc
    # Credentials come from the standard AWS chain (env vars, role), never from our config.
    return boto3.client("s3", region_name=marker.region, endpoint_url=marker.endpoint_url)


def _read_s3_marker(marker: S3Marker, factory: S3ClientFactory) -> datetime:
    try:
        head = factory(marker).head_object(Bucket=marker.bucket, Key=marker.key)
        modified: datetime = head["LastModified"]
    except BackupMarkerError:
        raise
    except Exception as exc:  # boto raises many unrelated types; all mean "cannot tell"
        raise BackupMarkerError(
            f"cannot read s3://{marker.bucket}/{marker.key}: {type(exc).__name__}"
        ) from exc
    return modified if modified.tzinfo else modified.replace(tzinfo=UTC)


def check_backup(
    cfg: BackupConfig,
    now: datetime | None = None,
    s3_factory: S3ClientFactory = _default_s3_client,
) -> CheckResult | None:
    """Returns None when no backup marker is configured (the check is then skipped)."""
    if not cfg.enabled:
        return None
    now = now or datetime.now(UTC)
    try:
        if cfg.file is not None:
            taken = _read_file_marker(cfg.file)
        elif cfg.s3 is not None:
            taken = _read_s3_marker(cfg.s3, s3_factory)
        else:  # unreachable: `enabled` means one of the two is set
            return None
    except BackupMarkerError as exc:
        return CheckResult("backup", Status.CRIT, str(exc))

    age = max((now - taken).total_seconds(), 0.0)
    status = grade(age, cfg.warn_hours * 3600, cfg.crit_hours * 3600)
    return CheckResult(
        "backup",
        status,
        f"last successful backup {human_duration(age)} ago",
        (Sample("pgsentry_backup_age_seconds", age, help="Seconds since the last good backup"),),
        {"last_backup": taken.isoformat(), "age_seconds": round(age)},
    )
