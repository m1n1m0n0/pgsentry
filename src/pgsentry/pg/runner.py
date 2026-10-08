"""Runs every applicable check against one PostgreSQL instance."""

from __future__ import annotations

import logging
from collections.abc import Callable

import psycopg

from pgsentry.config import Config
from pgsentry.models import CheckResult, Sample, Status
from pgsentry.pg import checks
from pgsentry.pg.backup import check_backup
from pgsentry.pg.client import PgClient, Querier

log = logging.getLogger(__name__)

Connect = Callable[..., PgClient]


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _guarded(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """One failing query (say, a missing GRANT) must not hide the other checks."""
    try:
        return fn()
    except psycopg.Error as exc:
        log.warning("check %s failed: %s", name, _first_line(exc))
        return CheckResult(name, Status.CRIT, f"query failed: {_first_line(exc)}")


def run_checks_on(db: Querier, cfg: Config) -> list[CheckResult]:
    """All database checks for an already-open connection."""
    is_replica, version = checks.fetch_is_replica(db)
    results = [checks.check_role(is_replica, version, cfg)]
    if is_replica:
        results.append(_guarded("wal_receiver", lambda: checks.check_wal_receiver(db, cfg)))
    else:
        results.append(_guarded("replication", lambda: checks.check_replication(db, cfg)))
    results.append(_guarded("slots", lambda: checks.check_slots(db, cfg, is_replica)))
    results.append(_guarded("connections", lambda: checks.check_connections(db, cfg)))
    results.append(_guarded("long_transactions", lambda: checks.check_long_transactions(db, cfg)))
    return results


def run_db_checks(
    cfg: Config, password: str | None, connect: Connect | None = None
) -> list[CheckResult]:
    connect = connect or PgClient.connect  # looked up at call time so tests can patch it
    pg = cfg.postgres
    target = f"{pg.host}:{pg.port}/{pg.dbname}"
    results: list[CheckResult]
    try:
        with connect(pg, password) as db:
            results = [
                CheckResult(
                    "connection",
                    Status.OK,
                    f"connected to {target} as {pg.user}",
                    (Sample("pgsentry_up", 1.0, help="1 if pgsentry could query the database"),),
                )
            ]
            results.extend(run_checks_on(db, cfg))
    except psycopg.Error as exc:
        log.error("cannot check %s: %s", target, _first_line(exc))
        results = [
            CheckResult(
                "connection",
                Status.CRIT,
                f"cannot query {target}: {_first_line(exc)}",
                (Sample("pgsentry_up", 0.0, help="1 if pgsentry could query the database"),),
            )
        ]
    # The backup marker is independent of the database, so it is checked either way.
    backup = check_backup(cfg.backup)
    if backup is not None:
        results.append(backup)
    return results
