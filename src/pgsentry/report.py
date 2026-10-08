"""Turning results into a table (for people) or JSON (for scripts)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table

from pgsentry.models import CheckResult, Status, worst

STYLE = {Status.OK: "green", Status.WARN: "yellow", Status.CRIT: "bold red"}


def overall(results: Sequence[CheckResult]) -> Status:
    return worst(r.status for r in results)


def results_to_dict(results: Sequence[CheckResult]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "overall": overall(results).label,
        "checks": [r.to_dict() for r in results],
    }


def results_to_json(results: Sequence[CheckResult]) -> str:
    return json.dumps(results_to_dict(results), indent=2, default=str)


def results_table(title: str, results: Sequence[CheckResult]) -> Table:
    table = Table(title=title, title_justify="left", show_lines=False)
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Message", overflow="fold")
    for r in results:
        table.add_row(r.name, f"[{STYLE[r.status]}]{r.status.label}[/]", r.message)
    return table


def print_results(title: str, results: Sequence[CheckResult], console: Console) -> None:
    console.print(results_table(title, results))
    status = overall(results)
    console.print(f"Overall: [{STYLE[status]}]{status.label}[/]")
