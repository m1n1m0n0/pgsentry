from typing import Any

import psycopg
import pytest
from click.testing import CliRunner

from conftest import FakeDb
from pgsentry.cli import main
from pgsentry.config import Config
from pgsentry.models import Status, exit_code
from pgsentry.pg import checks
from pgsentry.pg.runner import run_checks_on, run_db_checks
from pgsentry.report import overall, results_to_dict

PRIMARY_RESPONSES: dict[str, list[dict[str, Any]]] = {
    checks.SQL_ROLE: [{"in_recovery": False, "server_version": "16.4"}],
    checks.SQL_REPLICATION: [],
    checks.SQL_SLOTS_PRIMARY: [],
    checks.SQL_CONNECTIONS: [{"used": 3, "max_connections": 100, "reserved": 3}],
    checks.SQL_LONG_TRANSACTIONS: [],
}


def test_primary_runs_replication_not_wal_receiver(cfg: Config) -> None:
    results = run_checks_on(FakeDb(PRIMARY_RESPONSES), cfg)
    names = [r.name for r in results]
    assert names == ["role", "replication", "slots", "connections", "long_transactions"]
    assert overall(results) == Status.OK


def test_standby_runs_wal_receiver_not_replication(cfg: Config) -> None:
    responses = {
        checks.SQL_ROLE: [{"in_recovery": True, "server_version": "16.4"}],
        checks.SQL_WAL_RECEIVER: [],
        checks.SQL_SLOTS_STANDBY: [],
        checks.SQL_CONNECTIONS: PRIMARY_RESPONSES[checks.SQL_CONNECTIONS],
        checks.SQL_LONG_TRANSACTIONS: [],
    }
    results = run_checks_on(FakeDb(responses), cfg)
    assert [r.name for r in results][:2] == ["role", "wal_receiver"]
    assert overall(results) == Status.CRIT  # no receiver


def test_one_failing_query_does_not_hide_the_others(cfg: Config) -> None:
    class DeniedOnSlots(FakeDb):
        def query(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
            if sql == checks.SQL_SLOTS_PRIMARY:
                raise psycopg.errors.InsufficientPrivilege("permission denied for view\nHINT: x")
            return super().query(sql, params)

    results = {r.name: r for r in run_checks_on(DeniedOnSlots(PRIMARY_RESPONSES), cfg)}
    assert results["slots"].status == Status.CRIT
    assert results["slots"].message == "query failed: permission denied for view"
    assert results["connections"].status == Status.OK


def test_unreachable_database_is_one_critical_row_and_pgsentry_up_zero(cfg: Config) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    results = run_db_checks(cfg, None, connect=refuse)
    assert len(results) == 1
    assert results[0].status == Status.CRIT
    assert results[0].samples[0].name == "pgsentry_up"
    assert results[0].samples[0].value == 0


def test_json_report_shape(cfg: Config) -> None:
    results = run_checks_on(FakeDb(PRIMARY_RESPONSES), cfg)
    data = results_to_dict(results)
    assert data["overall"] == "OK"
    assert {c["check"] for c in data["checks"]} >= {"role", "replication"}


@pytest.mark.parametrize(
    ("status", "fail_on", "expected"),
    [
        (Status.OK, "warn", 0),
        (Status.WARN, "crit", 0),
        (Status.WARN, "warn", 1),
        (Status.CRIT, "crit", 2),
        (Status.CRIT, "warn", 2),
        (Status.CRIT, "never", 0),
    ],
)
def test_exit_codes(status: Status, fail_on: str, expected: int) -> None:
    assert exit_code(status, fail_on) == expected


def test_db_check_command_exits_2_when_database_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("pgsentry.pg.client.PgClient.connect", refuse)
    result = CliRunner().invoke(main, ["db", "check", "--format", "json"])
    assert result.exit_code == 2
    assert '"overall": "CRIT"' in result.output
