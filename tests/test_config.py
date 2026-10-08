from pathlib import Path

import pytest

from pgsentry.config import ConfigError, load_config, load_password


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file() -> None:
    config = load_config(None, env={})
    assert config.postgres.host == "localhost"
    assert config.thresholds.replication_lag_seconds.crit == 300
    assert not config.backup.enabled


def test_yaml_values_are_loaded(tmp_path: Path) -> None:
    path = write(tmp_path, "postgres:\n  host: db1\n  port: 5433\n  expected_role: primary\n")
    config = load_config(path, env={})
    assert (config.postgres.host, config.postgres.port) == ("db1", 5433)
    assert config.postgres.expected_role == "primary"


def test_env_overrides_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "postgres:\n  host: from-file\n")
    config = load_config(path, env={"PGSENTRY_DB_HOST": "from-env", "PGSENTRY_DB_PORT": "6543"})
    assert config.postgres.host == "from-env"
    assert config.postgres.port == 6543


def test_password_key_in_yaml_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "postgres:\n  password: hunter2\n")
    with pytest.raises(ConfigError, match=r"postgres\.password"):
        load_config(path, env={})


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, "bogus: 1\n"), env={})


def test_warn_above_crit_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "thresholds:\n  replication_lag_seconds: {warn: 10, crit: 5}\n")
    with pytest.raises(ConfigError, match="warn must be <= crit"):
        load_config(path, env={})


def test_backup_file_and_s3_are_mutually_exclusive(tmp_path: Path) -> None:
    text = "backup:\n  file: /x\n  s3: {bucket: b, key: k}\n"
    with pytest.raises(ConfigError, match="not both"):
        load_config(write(tmp_path, text), env={})


def test_audit_in_serve_needs_a_namespace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="namespace"):
        load_config(write(tmp_path, "kubernetes:\n  audit_in_serve: true\n"), env={})


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml", env={})


def test_password_from_env_wins_over_file(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("from-file\n", encoding="utf-8")
    env = {"PGSENTRY_DB_PASSWORD": "from-env", "PGSENTRY_DB_PASSWORD_FILE": str(secret)}
    assert load_password(env) == "from-env"


def test_password_from_file_strips_the_trailing_newline(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n", encoding="utf-8")
    assert load_password({"PGSENTRY_DB_PASSWORD_FILE": str(secret)}) == "s3cret"


def test_no_password_configured_returns_none() -> None:
    assert load_password({}) is None
