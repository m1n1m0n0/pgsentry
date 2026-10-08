from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from pgsentry.config import Config, load_config


class FakeDb:
    """Stands in for PgClient: returns canned rows keyed by the SQL constant that was run."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.seen: list[str] = []

    def query(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]:
        self.seen.append(sql)
        return self.responses[sql]


@pytest.fixture
def cfg() -> Config:
    return load_config(None, env={})


def dec(value: float) -> Decimal:
    """psycopg hands numeric columns back as Decimal; tests should exercise that."""
    return Decimal(str(value))
