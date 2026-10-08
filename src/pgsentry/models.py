"""Result types shared by every check, the CLI report and the Prometheus exporter."""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


class Status(enum.IntEnum):
    """Ordered so that max() of several statuses is the worst one."""

    OK = 0
    WARN = 1
    CRIT = 2

    @property
    def label(self) -> str:
        return self.name


def worst(statuses: Iterable[Status]) -> Status:
    return max(statuses, default=Status.OK)


def grade(value: float, warn: float, crit: float) -> Status:
    """Grade a 'higher is worse' number against two thresholds."""
    if value >= crit:
        return Status.CRIT
    if value >= warn:
        return Status.WARN
    return Status.OK


@dataclass(frozen=True)
class Sample:
    """One Prometheus gauge sample produced by a check."""

    name: str
    value: float
    labels: Mapping[str, str] = field(default_factory=dict)
    help: str = ""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    message: str
    samples: tuple[Sample, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status.label,
            "message": self.message,
            "details": dict(self.details),
        }


def exit_code(status: Status, fail_on: str) -> int:
    """Map the worst status to a process exit code.

    fail_on is "warn", "crit" or "never". With "crit" a WARN still exits 0, so a
    CI job only breaks on the things that are actually broken.
    """
    if fail_on == "never":
        return 0
    if status == Status.CRIT:
        return 2
    if status == Status.WARN and fail_on == "warn":
        return 1
    return 0
