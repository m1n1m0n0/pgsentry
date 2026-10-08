"""Configuration: a YAML file, a few environment overrides, secrets from the environment only.

The password is deliberately not a config field. Every model forbids unknown keys, so a
`password:` line in the YAML is rejected instead of being silently accepted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MIB = 1024**2
GIB = 1024**3

PASSWORD_ENV = "PGSENTRY_DB_PASSWORD"  # noqa: S105 - the name of a variable, not a secret
PASSWORD_FILE_ENV = "PGSENTRY_DB_PASSWORD_FILE"  # noqa: S105

# environment variable -> (section, field)
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "PGSENTRY_DB_HOST": ("postgres", "host"),
    "PGSENTRY_DB_PORT": ("postgres", "port"),
    "PGSENTRY_DB_NAME": ("postgres", "dbname"),
    "PGSENTRY_DB_USER": ("postgres", "user"),
    "PGSENTRY_DB_SSLMODE": ("postgres", "sslmode"),
    "PGSENTRY_LOG_LEVEL": ("logging", "level"),
}


class ConfigError(Exception):
    """Raised for anything wrong with the configuration; the CLI exits with code 3."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PostgresConfig(_Strict):
    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = "postgres"
    user: str = "pgsentry"
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    connect_timeout: int = Field(default=5, ge=1)
    statement_timeout_ms: int = Field(default=5000, ge=100)
    # If set, a mismatch is CRIT: catches an unplanned failover or a promoted replica.
    expected_role: Literal["primary", "replica"] | None = None
    # How many streaming replicas the primary should have. 0 means "do not check".
    expected_replicas: int = Field(default=0, ge=0)


class Threshold(_Strict):
    warn: float
    crit: float

    @model_validator(mode="after")
    def _warn_not_above_crit(self) -> Self:
        if self.warn > self.crit:
            raise ValueError("warn must be <= crit")
        return self


class Thresholds(_Strict):
    replication_lag_bytes: Threshold = Threshold(warn=16 * MIB, crit=256 * MIB)
    replication_lag_seconds: Threshold = Threshold(warn=30, crit=300)
    slot_retained_bytes: Threshold = Threshold(warn=1 * GIB, crit=10 * GIB)
    connection_usage_ratio: Threshold = Threshold(warn=0.80, crit=0.95)
    long_transaction_seconds: Threshold = Threshold(warn=300, crit=3600)


class S3Marker(_Strict):
    bucket: str
    key: str
    region: str | None = None
    endpoint_url: str | None = None


class BackupConfig(_Strict):
    """Where to find the 'last successful backup' marker. Credentials for S3 come from
    the standard AWS environment variables / role, never from this file."""

    file: Path | None = None
    s3: S3Marker | None = None
    warn_hours: float = Field(default=26, gt=0)
    crit_hours: float = Field(default=50, gt=0)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if self.file and self.s3:
            raise ValueError("set either backup.file or backup.s3, not both")
        if self.warn_hours > self.crit_hours:
            raise ValueError("warn_hours must be <= crit_hours")
        return self

    @property
    def enabled(self) -> bool:
        return self.file is not None or self.s3 is not None


class KubernetesConfig(_Strict):
    namespace: str | None = None
    kubeconfig: Path | None = None
    context: str | None = None
    # `pgsentry serve` runs the pod audit each cycle only when this is true.
    audit_in_serve: bool = False

    @model_validator(mode="after")
    def _serve_needs_namespace(self) -> Self:
        if self.audit_in_serve and not self.namespace:
            raise ValueError("kubernetes.audit_in_serve requires kubernetes.namespace")
        return self


class ServeConfig(_Strict):
    # Loopback by default; the Kubernetes manifest and the lab config opt in to 0.0.0.0.
    bind: str = "127.0.0.1"
    port: int = Field(default=9188, ge=1, le=65535)
    interval_seconds: float = Field(default=30, ge=1)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_format: bool = False


class Config(_Strict):
    postgres: PostgresConfig = PostgresConfig()
    thresholds: Thresholds = Thresholds()
    backup: BackupConfig = BackupConfig()
    kubernetes: KubernetesConfig = KubernetesConfig()
    serve: ServeConfig = ServeConfig()
    logging: LoggingConfig = LoggingConfig()


def load_config(path: Path | None, env: Mapping[str, str] | None = None) -> Config:
    """Read the YAML file (if any), apply environment overrides, validate."""
    env = os.environ if env is None else env
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ConfigError(f"{path}: top level must be a mapping")
            raw = loaded

    for var, (section, name) in ENV_OVERRIDES.items():
        if var in env:
            section_data = raw.setdefault(section, {})
            if not isinstance(section_data, dict):
                raise ConfigError(f"{path}: '{section}' must be a mapping")
            section_data[name] = env[var]

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"invalid configuration: {problems}") from exc


def load_password(env: Mapping[str, str] | None = None) -> str | None:
    """The DB password comes from an env var or a mounted file, in that order.

    Returns None when neither is set; libpq then falls back to ~/.pgpass / PGPASSFILE
    or an auth method that needs no password.
    """
    env = os.environ if env is None else env
    if env.get(PASSWORD_ENV):
        return env[PASSWORD_ENV]
    file_path = env.get(PASSWORD_FILE_ENV)
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(
                f"cannot read {PASSWORD_FILE_ENV} {file_path}: {exc.strerror}"
            ) from exc
    return None
