"""A thin wrapper around a psycopg connection.

The checks only depend on the `Querier` protocol, so tests can hand them a fake object.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any, Protocol, Self

import psycopg
from psycopg.rows import dict_row

from pgsentry.config import PostgresConfig


class Querier(Protocol):
    def query(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]: ...


class PgClient:
    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn

    @classmethod
    def connect(cls, cfg: PostgresConfig, password: str | None) -> Self:
        # Defence in depth on top of the pg_monitor role: the session itself is read-only,
        # and no single query may run longer than statement_timeout_ms.
        options = (
            f"-c default_transaction_read_only=on -c statement_timeout={cfg.statement_timeout_ms}"
        )
        conn = psycopg.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.dbname,
            user=cfg.user,
            password=password,
            sslmode=cfg.sslmode,
            connect_timeout=cfg.connect_timeout,
            application_name="pgsentry",
            options=options,
            autocommit=True,  # no transaction left open, nothing to abort after an error
            row_factory=dict_row,
        )
        return cls(conn)

    def query(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)  # SQL is always one of the module constants
            return list(cur.fetchall())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
