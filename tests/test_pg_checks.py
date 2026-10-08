from typing import Any

from conftest import FakeDb, dec
from pgsentry.config import Config, load_config
from pgsentry.models import Status
from pgsentry.pg import checks

MIB = 1024**2


def replica_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "application_name": "replica1",
        "client_addr": "172.18.0.3",
        "state": "streaming",
        "sync_state": "async",
        "replay_lag_bytes": dec(0),
        "replay_lag_seconds": dec(0.002),
    }
    row.update(overrides)
    return row


# --- role -------------------------------------------------------------------------------


def test_role_primary_ok(cfg: Config) -> None:
    result = checks.check_role(False, "16.4", cfg)
    assert result.status == Status.OK
    assert "primary" in result.message


def test_role_mismatch_is_critical() -> None:
    cfg = load_config(None, env={})
    cfg.postgres.expected_role = "primary"
    result = checks.check_role(True, "16.4", cfg)
    assert result.status == Status.CRIT
    assert "expected primary" in result.message


# --- replication (primary side) ---------------------------------------------------------


def test_replication_healthy(cfg: Config) -> None:
    cfg.postgres.expected_replicas = 1
    db = FakeDb({checks.SQL_REPLICATION: [replica_row()]})
    result = checks.check_replication(db, cfg)
    assert result.status == Status.OK
    names = {s.name for s in result.samples}
    assert "pgsentry_replication_lag_bytes" in names


def test_replication_missing_replica_is_critical(cfg: Config) -> None:
    cfg.postgres.expected_replicas = 1
    db = FakeDb({checks.SQL_REPLICATION: []})
    result = checks.check_replication(db, cfg)
    assert result.status == Status.CRIT
    assert "0 of 1" in result.message
    streaming = next(s for s in result.samples if s.name.endswith("streaming_replicas"))
    assert streaming.value == 0
    expected = next(s for s in result.samples if s.name.endswith("expected_replicas"))
    assert expected.value == 1


def test_replication_no_replicas_expected_is_ok(cfg: Config) -> None:
    db = FakeDb({checks.SQL_REPLICATION: []})
    assert checks.check_replication(db, cfg).status == Status.OK


def test_replication_byte_lag_thresholds(cfg: Config) -> None:
    warn = checks.check_replication(
        FakeDb({checks.SQL_REPLICATION: [replica_row(replay_lag_bytes=dec(20 * MIB))]}), cfg
    )
    crit = checks.check_replication(
        FakeDb({checks.SQL_REPLICATION: [replica_row(replay_lag_bytes=dec(300 * MIB))]}), cfg
    )
    assert warn.status == Status.WARN
    assert crit.status == Status.CRIT


def test_replication_time_lag_threshold(cfg: Config) -> None:
    rows = [replica_row(replay_lag_seconds=dec(400))]
    assert (
        checks.check_replication(FakeDb({checks.SQL_REPLICATION: rows}), cfg).status == Status.CRIT
    )


def test_replication_message_names_the_metric_that_crossed_the_threshold(cfg: Config) -> None:
    # right after a pause is lifted: no bytes behind, but PG's own time estimate is stale
    rows = [replica_row(replay_lag_bytes=dec(0), replay_lag_seconds=dec(400))]
    result = checks.check_replication(FakeDb({checks.SQL_REPLICATION: rows}), cfg)
    assert result.status == Status.CRIT
    assert "replay lag 6m" in result.message
    assert "0 B" not in result.message


def test_replication_idle_standby_has_null_lag_and_is_ok(cfg: Config) -> None:
    rows = [replica_row(replay_lag_bytes=None, replay_lag_seconds=None)]
    result = checks.check_replication(FakeDb({checks.SQL_REPLICATION: rows}), cfg)
    assert result.status == Status.OK


def test_replication_catchup_state_warns(cfg: Config) -> None:
    rows = [replica_row(state="catchup")]
    result = checks.check_replication(FakeDb({checks.SQL_REPLICATION: rows}), cfg)
    assert result.status == Status.WARN
    assert "catchup" in result.message


# --- WAL receiver (standby side) --------------------------------------------------------

RECEIVER_OK = {
    "status": "streaming",
    "sender_host": "primary",
    "sender_port": 5432,
    "slot_name": "replica1_slot",
    "last_msg_age_seconds": dec(0.4),
}


def replay_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "replay_gap_bytes": dec(0),
        "replay_lag_seconds": 0,
        "replay_paused": False,
    }
    row.update(overrides)
    return row


def test_wal_receiver_healthy(cfg: Config) -> None:
    db = FakeDb({checks.SQL_WAL_RECEIVER: [RECEIVER_OK], checks.SQL_REPLAY: [replay_row()]})
    result = checks.check_wal_receiver(db, cfg)
    assert result.status == Status.OK
    assert "streaming from primary:5432" in result.message


def test_wal_receiver_absent_is_critical(cfg: Config) -> None:
    db = FakeDb({checks.SQL_WAL_RECEIVER: []})
    result = checks.check_wal_receiver(db, cfg)
    assert result.status == Status.CRIT
    assert checks.SQL_REPLAY not in db.seen  # no point asking about replay without a receiver
    assert result.samples[0].value == 0


def test_wal_receiver_paused_replay_warns(cfg: Config) -> None:
    db = FakeDb(
        {
            checks.SQL_WAL_RECEIVER: [RECEIVER_OK],
            checks.SQL_REPLAY: [replay_row(replay_paused=True)],
        }
    )
    result = checks.check_wal_receiver(db, cfg)
    assert result.status == Status.WARN
    assert "paused" in result.message


def test_wal_receiver_growing_gap_goes_critical(cfg: Config) -> None:
    replay = replay_row(
        replay_gap_bytes=dec(500 * MIB), replay_lag_seconds=dec(45), replay_paused=True
    )
    db = FakeDb({checks.SQL_WAL_RECEIVER: [RECEIVER_OK], checks.SQL_REPLAY: [replay]})
    result = checks.check_wal_receiver(db, cfg)
    assert result.status == Status.CRIT
    assert "not replayed" in result.message


def test_wal_receiver_unknown_time_lag_is_not_reported_as_zero(cfg: Config) -> None:
    # pg_last_xact_replay_timestamp() is NULL until a commit has been replayed
    replay = replay_row(replay_gap_bytes=dec(70 * MIB), replay_lag_seconds=None, replay_paused=True)
    db = FakeDb({checks.SQL_WAL_RECEIVER: [RECEIVER_OK], checks.SQL_REPLAY: [replay]})
    result = checks.check_wal_receiver(db, cfg)
    assert result.status == Status.WARN
    assert "0.0s" not in result.message
    assert all(s.name != "pgsentry_replica_replay_lag_seconds" for s in result.samples)


def test_wal_receiver_not_streaming_status_warns(cfg: Config) -> None:
    receiver = {**RECEIVER_OK, "status": "starting"}
    db = FakeDb({checks.SQL_WAL_RECEIVER: [receiver], checks.SQL_REPLAY: [replay_row()]})
    assert checks.check_wal_receiver(db, cfg).status == Status.WARN


# --- slots ------------------------------------------------------------------------------


def slot_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "slot_name": "replica1_slot",
        "slot_type": "physical",
        "active": True,
        "wal_status": "reserved",
        "retained_bytes": dec(1024),
    }
    row.update(overrides)
    return row


def test_slots_none(cfg: Config) -> None:
    assert (
        checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: []}), cfg, False).status == Status.OK
    )


def test_slots_healthy(cfg: Config) -> None:
    result = checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: [slot_row()]}), cfg, False)
    assert result.status == Status.OK


def test_slots_inactive_warns(cfg: Config) -> None:
    rows = [slot_row(active=False)]
    result = checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: rows}), cfg, False)
    assert result.status == Status.WARN
    assert "inactive" in result.message


def test_slots_retaining_too_much_is_critical(cfg: Config) -> None:
    rows = [slot_row(active=False, retained_bytes=dec(20 * 1024**3))]
    result = checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: rows}), cfg, False)
    assert result.status == Status.CRIT


def test_slots_lost_is_critical(cfg: Config) -> None:
    rows = [slot_row(wal_status="lost")]
    assert (
        checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: rows}), cfg, False).status
        == Status.CRIT
    )


def test_slots_on_standby_use_the_replay_lsn_query(cfg: Config) -> None:
    db = FakeDb({checks.SQL_SLOTS_STANDBY: []})
    checks.check_slots(db, cfg, True)
    assert db.seen == [checks.SQL_SLOTS_STANDBY]
    assert "pg_current_wal_lsn" not in checks.SQL_SLOTS_STANDBY  # errors during recovery


def test_slot_without_restart_lsn_has_no_byte_sample(cfg: Config) -> None:
    rows = [slot_row(retained_bytes=None)]
    result = checks.check_slots(FakeDb({checks.SQL_SLOTS_PRIMARY: rows}), cfg, False)
    assert all("retained" not in s.name for s in result.samples)


# --- connections ------------------------------------------------------------------------


def test_connections_ratio_uses_usable_slots(cfg: Config) -> None:
    db = FakeDb({checks.SQL_CONNECTIONS: [{"used": 50, "max_connections": 103, "reserved": 3}]})
    result = checks.check_connections(db, cfg)
    assert result.status == Status.OK
    assert result.details["usable"] == 100
    assert result.details["ratio"] == 0.5


def test_connections_thresholds(cfg: Config) -> None:
    def run(used: int) -> Status:
        row = {"used": used, "max_connections": 100, "reserved": 0}
        return checks.check_connections(FakeDb({checks.SQL_CONNECTIONS: [row]}), cfg).status

    assert run(79) == Status.OK
    assert run(80) == Status.WARN
    assert run(95) == Status.CRIT


# --- long transactions ------------------------------------------------------------------


def txn(age: float, state: str = "idle in transaction") -> dict[str, Any]:
    return {"pid": 4242, "usename": "app", "state": state, "xact_age_seconds": dec(age)}


def test_no_open_transactions(cfg: Config) -> None:
    result = checks.check_long_transactions(FakeDb({checks.SQL_LONG_TRANSACTIONS: []}), cfg)
    assert result.status == Status.OK
    assert result.message == "no open transactions"


def test_long_transaction_thresholds(cfg: Config) -> None:
    def run(age: float) -> Status:
        return checks.check_long_transactions(
            FakeDb({checks.SQL_LONG_TRANSACTIONS: [txn(age)]}), cfg
        ).status

    assert run(10) == Status.OK
    assert run(400) == Status.WARN
    assert run(4000) == Status.CRIT


def test_long_transaction_message_names_the_session_but_not_the_query(cfg: Config) -> None:
    result = checks.check_long_transactions(
        FakeDb({checks.SQL_LONG_TRANSACTIONS: [txn(4000)]}), cfg
    )
    assert "pid 4242" in result.message
    assert "query" not in checks.SQL_LONG_TRANSACTIONS.split("SELECT", 1)[1].split("FROM")[0]
