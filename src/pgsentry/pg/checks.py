"""The PostgreSQL health checks.

Every check is a small function: run one query, turn the rows into a CheckResult.
The SQL lives in module constants so each query can be read (and tested) on its own.
All queries are read-only and work for a role that is only a member of `pg_monitor`.
"""

from __future__ import annotations

from typing import Any

from pgsentry.config import Config
from pgsentry.formatting import human_bytes, human_duration
from pgsentry.models import CheckResult, Sample, Status, grade, worst
from pgsentry.pg.client import Querier

# Is this server a primary or a standby? Everything else branches on the answer.
SQL_ROLE = """
SELECT pg_is_in_recovery() AS in_recovery,
       current_setting('server_version') AS server_version
"""

# Primary only. One row per connected walsender, i.e. per standby (and per
# pg_basebackup, which has state 'backup' and is not a replica).
#   replay_lsn          - how far the standby has applied WAL
#   pg_current_wal_lsn  - how far the primary has written WAL
#   the difference      - bytes the standby still has to replay
#   replay_lag          - PG's own time estimate (interval); NULL when the standby is idle
SQL_REPLICATION = """
SELECT application_name,
       client_addr::text AS client_addr,
       state,
       sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
       EXTRACT(EPOCH FROM replay_lag) AS replay_lag_seconds
FROM pg_stat_replication
WHERE state <> 'backup'
ORDER BY application_name
"""

# Standby only. The standby's own view of its connection to the primary. One row while a
# WAL receiver process exists, zero rows when it is not running at all.
SQL_WAL_RECEIVER = """
SELECT status,
       sender_host,
       sender_port,
       slot_name,
       EXTRACT(EPOCH FROM now() - last_msg_receipt_time) AS last_msg_age_seconds
FROM pg_stat_wal_receiver
"""

# Standby only. received - replayed = WAL on disk that has not been applied yet.
# The time lag is only meaningful while there is something left to replay: on an idle
# primary now() - last_replay_timestamp keeps growing although nothing is behind.
SQL_REPLAY = """
SELECT pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()) AS replay_gap_bytes,
       CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()
            THEN 0
            ELSE EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())
       END AS replay_lag_seconds,
       pg_is_wal_replay_paused() AS replay_paused
"""

# Replication slots. A slot makes the primary keep WAL until the consumer has read it,
# so an inactive slot is a disk that slowly fills up. Two variants because
# pg_current_wal_lsn() raises an error on a standby.
_SLOTS_COLUMNS = """
SELECT slot_name, slot_type, active, wal_status,
       pg_wal_lsn_diff({lsn}, restart_lsn) AS retained_bytes
FROM pg_replication_slots
ORDER BY slot_name
"""
SQL_SLOTS_PRIMARY = _SLOTS_COLUMNS.format(lsn="pg_current_wal_lsn()")
SQL_SLOTS_STANDBY = _SLOTS_COLUMNS.format(lsn="pg_last_wal_replay_lsn()")

# Client sessions plus walsenders count against max_connections; the last
# superuser_reserved_connections slots are kept for superusers.
SQL_CONNECTIONS = """
SELECT count(*) FILTER (WHERE backend_type IN ('client backend', 'walsender')) AS used,
       current_setting('max_connections')::int AS max_connections,
       current_setting('superuser_reserved_connections')::int AS reserved
FROM pg_stat_activity
"""

# Oldest open transactions (including 'idle in transaction'), excluding ourselves.
# A long transaction holds back VACUUM and can block DDL. The query text is not
# selected on purpose: it can contain data and does not belong in a report.
SQL_LONG_TRANSACTIONS = """
SELECT pid,
       usename,
       state,
       EXTRACT(EPOCH FROM now() - xact_start) AS xact_age_seconds
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
  AND backend_type = 'client backend'
  AND pid <> pg_backend_pid()
ORDER BY xact_start
LIMIT 5
"""


def _num(value: Any) -> float | None:
    """psycopg returns numeric as Decimal and SQL NULL as None."""
    return None if value is None else float(value)


def _receiver_sample(streaming: bool) -> Sample:
    return Sample(
        "pgsentry_replica_wal_receiver_streaming",
        1.0 if streaming else 0.0,
        help="1 if this standby's WAL receiver is streaming from its primary",
    )


def fetch_is_replica(db: Querier) -> tuple[bool, str]:
    row = db.query(SQL_ROLE)[0]
    return bool(row["in_recovery"]), str(row["server_version"])


def check_role(is_replica: bool, version: str, cfg: Config) -> CheckResult:
    role = "replica" if is_replica else "primary"
    expected = cfg.postgres.expected_role
    sample = Sample("pgsentry_is_replica", 1.0 if is_replica else 0.0, help="1 if in recovery")
    if expected and expected != role:
        return CheckResult(
            "role",
            Status.CRIT,
            f"running as {role} but expected {expected} (PostgreSQL {version})",
            (sample,),
            {"role": role, "expected": expected},
        )
    return CheckResult(
        "role", Status.OK, f"{role} (PostgreSQL {version})", (sample,), {"role": role}
    )


def check_replication(db: Querier, cfg: Config) -> CheckResult:
    """Primary-side view: which standbys are connected and how far behind they are."""
    rows = db.query(SQL_REPLICATION)
    streaming = [r for r in rows if r["state"] == "streaming"]
    th = cfg.thresholds
    samples = [
        Sample(
            "pgsentry_replication_streaming_replicas",
            float(len(streaming)),
            help="Standbys currently in the streaming state",
        )
    ]
    statuses: list[Status] = []
    problems: list[str] = []
    details: list[dict[str, Any]] = []

    expected = cfg.postgres.expected_replicas
    if len(streaming) < expected:
        statuses.append(Status.CRIT)
        problems.append(f"only {len(streaming)} of {expected} expected replicas streaming")

    for r in rows:
        name = str(r["application_name"])
        lag_bytes = _num(r["replay_lag_bytes"])
        lag_seconds = _num(r["replay_lag_seconds"])
        status = Status.OK
        if r["state"] != "streaming":
            status = Status.WARN
            problems.append(f"{name} is in state '{r['state']}'")
        if lag_bytes is not None:
            status = max(
                status,
                grade(lag_bytes, th.replication_lag_bytes.warn, th.replication_lag_bytes.crit),
            )
            samples.append(
                Sample(
                    "pgsentry_replication_lag_bytes",
                    lag_bytes,
                    {"replica": name},
                    "WAL bytes the standby has yet to replay (primary's view)",
                )
            )
        if lag_seconds is not None:
            status = max(
                status,
                grade(
                    lag_seconds,
                    th.replication_lag_seconds.warn,
                    th.replication_lag_seconds.crit,
                ),
            )
            samples.append(
                Sample(
                    "pgsentry_replication_lag_seconds",
                    lag_seconds,
                    {"replica": name},
                    "Replay lag reported by the primary",
                )
            )
        if status != Status.OK and lag_bytes is not None:
            problems.append(f"{name} replay lag {human_bytes(lag_bytes)}")
        statuses.append(status)
        details.append(
            {
                "replica": name,
                "client_addr": r["client_addr"],
                "state": r["state"],
                "sync_state": r["sync_state"],
                "replay_lag_bytes": lag_bytes,
                "replay_lag_seconds": lag_seconds,
            }
        )

    overall = worst(statuses)
    if problems:
        message = "; ".join(problems)
    elif rows:
        worst_bytes = max((_num(r["replay_lag_bytes"]) or 0.0 for r in rows), default=0.0)
        message = (
            f"{len(streaming)} replica(s) streaming, max replay lag {human_bytes(worst_bytes)}"
        )
    else:
        message = "no replicas connected (none expected)"
    return CheckResult("replication", overall, message, tuple(samples), {"replicas": details})


def check_wal_receiver(db: Querier, cfg: Config) -> CheckResult:
    """Standby-side view: is my connection to the primary up, and how far behind am I?

    This is the only check that can notice a standby that lost its primary, because the
    primary's pg_stat_replication simply loses the row instead of reporting a problem.
    """
    th = cfg.thresholds
    receiver = db.query(SQL_WAL_RECEIVER)
    if not receiver:
        return CheckResult(
            "wal_receiver",
            Status.CRIT,
            "no WAL receiver process: this standby is not connected to a primary",
            (_receiver_sample(False),),
        )

    row = receiver[0]
    replay = db.query(SQL_REPLAY)[0]
    gap = _num(replay["replay_gap_bytes"]) or 0.0
    lag_seconds = _num(replay["replay_lag_seconds"]) or 0.0
    paused = bool(replay["replay_paused"])

    statuses = [
        Status.OK if row["status"] == "streaming" else Status.WARN,
        grade(gap, th.replication_lag_bytes.warn, th.replication_lag_bytes.crit),
        grade(lag_seconds, th.replication_lag_seconds.warn, th.replication_lag_seconds.crit),
    ]
    problems: list[str] = []
    if row["status"] != "streaming":
        problems.append(f"receiver status is '{row['status']}'")
    if paused:
        statuses.append(Status.WARN)
        problems.append("WAL replay is paused")
    if max(statuses[1:]) != Status.OK:
        problems.append(
            f"{human_bytes(gap)} received but not replayed ({human_duration(lag_seconds)})"
        )

    sender = f"{row['sender_host']}:{row['sender_port']}"
    message = (
        "; ".join(problems)
        if problems
        else f"streaming from {sender}, replay gap {human_bytes(gap)}"
    )
    samples = (
        _receiver_sample(row["status"] == "streaming"),
        Sample(
            "pgsentry_replica_replay_lag_bytes",
            gap,
            help="WAL received but not yet replayed (standby's view)",
        ),
        Sample("pgsentry_replica_replay_lag_seconds", lag_seconds, help="Replay delay in seconds"),
        Sample("pgsentry_replica_replay_paused", 1.0 if paused else 0.0, help="1 if replay paused"),
    )
    return CheckResult(
        "wal_receiver",
        worst(statuses),
        message,
        samples,
        {
            "status": row["status"],
            "sender": f"{row['sender_host']}:{row['sender_port']}",
            "slot_name": row["slot_name"],
            "last_msg_age_seconds": _num(row["last_msg_age_seconds"]),
            "replay_gap_bytes": gap,
            "replay_lag_seconds": lag_seconds,
            "replay_paused": paused,
        },
    )


def check_slots(db: Querier, cfg: Config, is_replica: bool) -> CheckResult:
    th = cfg.thresholds.slot_retained_bytes
    rows = db.query(SQL_SLOTS_STANDBY if is_replica else SQL_SLOTS_PRIMARY)
    if not rows:
        return CheckResult("slots", Status.OK, "no replication slots")

    samples: list[Sample] = []
    statuses: list[Status] = []
    problems: list[str] = []
    details: list[dict[str, Any]] = []
    for r in rows:
        name = str(r["slot_name"])
        retained = _num(r["retained_bytes"])
        status = Status.OK
        if not r["active"]:
            status = Status.WARN
            problems.append(f"slot '{name}' is inactive")
        if r["wal_status"] == "lost":
            status = Status.CRIT
            problems.append(f"slot '{name}' lost required WAL")
        elif r["wal_status"] == "unreserved":
            status = max(status, Status.WARN)
            problems.append(f"slot '{name}' is about to lose required WAL")
        if retained is not None:
            level = grade(retained, th.warn, th.crit)
            if level != Status.OK:
                problems.append(f"slot '{name}' retains {human_bytes(retained)} of WAL")
            status = max(status, level)
            samples.append(
                Sample(
                    "pgsentry_replication_slot_retained_bytes",
                    retained,
                    {"slot": name},
                    "WAL bytes held back by the slot",
                )
            )
        samples.append(
            Sample(
                "pgsentry_replication_slot_active",
                1.0 if r["active"] else 0.0,
                {"slot": name},
                "1 if a consumer is connected to the slot",
            )
        )
        statuses.append(status)
        details.append(
            {
                "slot": name,
                "type": r["slot_type"],
                "active": bool(r["active"]),
                "wal_status": r["wal_status"],
                "retained_bytes": retained,
            }
        )
    message = (
        "; ".join(problems) if problems else f"{len(rows)} slot(s), all active, WAL retention ok"
    )
    return CheckResult("slots", worst(statuses), message, tuple(samples), {"slots": details})


def check_connections(db: Querier, cfg: Config) -> CheckResult:
    th = cfg.thresholds.connection_usage_ratio
    row = db.query(SQL_CONNECTIONS)[0]
    used = int(row["used"])
    usable = max(int(row["max_connections"]) - int(row["reserved"]), 1)
    ratio = used / usable
    status = grade(ratio, th.warn, th.crit)
    return CheckResult(
        "connections",
        status,
        f"{used} of {usable} usable connections in use ({ratio:.0%})",
        (
            Sample("pgsentry_connections_used", float(used), help="Client + walsender backends"),
            Sample(
                "pgsentry_connections_max", float(usable), help="max_connections minus reserved"
            ),
            Sample("pgsentry_connections_used_ratio", ratio, help="used / usable connections"),
        ),
        {"used": used, "usable": usable, "ratio": round(ratio, 4)},
    )


def check_long_transactions(db: Querier, cfg: Config) -> CheckResult:
    th = cfg.thresholds.long_transaction_seconds
    rows = db.query(SQL_LONG_TRANSACTIONS)
    oldest = _num(rows[0]["xact_age_seconds"]) if rows else 0.0
    oldest = oldest or 0.0
    status = grade(oldest, th.warn, th.crit)
    if status == Status.OK:
        message = (
            f"oldest open transaction {human_duration(oldest)}" if rows else "no open transactions"
        )
    else:
        top = rows[0]
        message = (
            f"transaction by '{top['usename']}' (pid {top['pid']}, {top['state']}) "
            f"open for {human_duration(oldest)}"
        )
    return CheckResult(
        "long_transactions",
        status,
        message,
        (Sample("pgsentry_longest_transaction_seconds", oldest, help="Age of oldest open txn"),),
        {
            "transactions": [
                {
                    "pid": r["pid"],
                    "user": r["usename"],
                    "state": r["state"],
                    "age_seconds": _num(r["xact_age_seconds"]),
                }
                for r in rows
            ]
        },
    )
