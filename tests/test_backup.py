from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pgsentry.config import BackupConfig, S3Marker
from pgsentry.models import Status
from pgsentry.pg.backup import check_backup

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def file_cfg(path: Path) -> BackupConfig:
    return BackupConfig(file=path)


def test_not_configured_is_skipped() -> None:
    assert check_backup(BackupConfig(), NOW) is None


def test_fresh_iso_timestamp_is_ok(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text((NOW - timedelta(hours=3)).isoformat(), encoding="utf-8")
    result = check_backup(file_cfg(marker), NOW)
    assert result is not None
    assert result.status == Status.OK
    assert result.details["age_seconds"] == 3 * 3600


def test_naive_timestamp_is_taken_as_utc(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text("2026-01-10T09:00:00", encoding="utf-8")
    result = check_backup(file_cfg(marker), NOW)
    assert result is not None
    assert result.details["age_seconds"] == 3 * 3600


def test_epoch_seconds_are_accepted(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text(str(int((NOW - timedelta(hours=30)).timestamp())), encoding="utf-8")
    result = check_backup(file_cfg(marker), NOW)
    assert result is not None
    assert result.status == Status.WARN  # 26h warn, 50h crit


def test_old_backup_is_critical(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text((NOW - timedelta(hours=60)).isoformat(), encoding="utf-8")
    result = check_backup(file_cfg(marker), NOW)
    assert result is not None
    assert result.status == Status.CRIT
    assert result.samples[0].name == "pgsentry_backup_age_seconds"


def test_empty_file_falls_back_to_mtime(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text("", encoding="utf-8")
    result = check_backup(file_cfg(marker))  # real "now": the file was just created
    assert result is not None
    assert result.status == Status.OK


def test_garbage_content_is_critical(tmp_path: Path) -> None:
    marker = tmp_path / "last_backup"
    marker.write_text("yesterday-ish", encoding="utf-8")
    result = check_backup(file_cfg(marker), NOW)
    assert result is not None
    assert result.status == Status.CRIT


def test_missing_file_is_critical(tmp_path: Path) -> None:
    result = check_backup(file_cfg(tmp_path / "nope"), NOW)
    assert result is not None
    assert result.status == Status.CRIT
    assert "not found" in result.message


class FakeS3:
    def __init__(self, modified: datetime | Exception) -> None:
        self.modified = modified
        self.calls: list[dict[str, str]] = []

    def head_object(self, **kwargs: str) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.modified, Exception):
            raise self.modified
        return {"LastModified": self.modified}


def test_s3_marker_age() -> None:
    fake = FakeS3(NOW - timedelta(hours=5))
    cfg = BackupConfig(s3=S3Marker(bucket="b", key="k"))
    result = check_backup(cfg, NOW, s3_factory=lambda _marker: fake)
    assert result is not None
    assert result.status == Status.OK
    assert fake.calls == [{"Bucket": "b", "Key": "k"}]


def test_s3_error_is_critical_and_does_not_leak_details() -> None:
    fake = FakeS3(RuntimeError("AccessDenied: arn:aws:iam::123456789012:role/secret"))
    cfg = BackupConfig(s3=S3Marker(bucket="b", key="k"))
    result = check_backup(cfg, NOW, s3_factory=lambda _marker: fake)
    assert result is not None
    assert result.status == Status.CRIT
    assert "123456789012" not in result.message
