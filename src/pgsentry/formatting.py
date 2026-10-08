"""Small helpers for turning numbers into readable text."""

from __future__ import annotations

from datetime import timedelta


def human_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f}s"
    delta = timedelta(seconds=int(seconds))
    hours, rest = divmod(delta.seconds, 3600)
    minutes = rest // 60
    parts = []
    if delta.days:
        parts.append(f"{delta.days}d")
    if hours or delta.days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
