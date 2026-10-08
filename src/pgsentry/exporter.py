"""Prometheus exporter: a background loop runs the checks, /metrics serves the last result.

A scrape never runs a query. Prometheus can scrape every 5 seconds without adding load to
the database, and a slow or failing check cannot make the scrape time out.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable

from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import CollectorRegistry

from pgsentry.config import ServeConfig
from pgsentry.models import CheckResult

log = logging.getLogger(__name__)

CollectFn = Callable[[], list[CheckResult]]


class SnapshotCollector:
    """Turns the latest list of CheckResults into gauges on every scrape.

    Building the families from a snapshot (instead of mutating long-lived Gauge objects)
    means a replica or slot that disappears also disappears from the metrics, rather than
    lingering with its last value.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: list[CheckResult] = []
        self._finished_at = 0.0
        self._duration = 0.0

    def update(self, results: list[CheckResult], duration: float) -> None:
        with self._lock:
            self._results = results
            self._finished_at = time.time()
            self._duration = duration

    def collect(self) -> Iterable[GaugeMetricFamily]:
        with self._lock:
            results, finished_at, duration = self._results, self._finished_at, self._duration

        status = GaugeMetricFamily(
            "pgsentry_check_status", "Check result: 0 OK, 1 WARN, 2 CRIT", labels=["check"]
        )
        for result in results:
            status.add_metric([result.name], float(result.status))
        yield status

        families: dict[str, GaugeMetricFamily] = {}
        for result in results:
            for sample in result.samples:
                label_names = sorted(sample.labels)
                family = families.get(sample.name)
                if family is None:
                    family = GaugeMetricFamily(
                        sample.name, sample.help or sample.name, labels=label_names
                    )
                    families[sample.name] = family
                family.add_metric([sample.labels[k] for k in label_names], sample.value)
        yield from families.values()

        yield GaugeMetricFamily(
            "pgsentry_last_run_timestamp_seconds",
            "Unix time the last check cycle finished (0 = none yet)",
            value=finished_at,
        )
        yield GaugeMetricFamily(
            "pgsentry_last_run_duration_seconds", "Duration of the last check cycle", value=duration
        )


def run_loop(
    collector: SnapshotCollector, collect: CollectFn, interval: float, stop: threading.Event
) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            results = collect()
        except Exception:  # the loop must survive anything a single cycle throws
            log.exception("check cycle failed")
            results = []
        collector.update(results, time.monotonic() - started)
        stop.wait(interval)


def serve(cfg: ServeConfig, collect: CollectFn, stop: threading.Event | None = None) -> None:
    """Start the HTTP server and block until `stop` is set (or forever)."""
    stop = stop or threading.Event()
    registry = CollectorRegistry()
    collector = SnapshotCollector()
    registry.register(collector)
    start_http_server(cfg.port, addr=cfg.bind, registry=registry)
    log.info(
        "serving metrics on http://%s:%d/metrics every %.0fs",
        cfg.bind,
        cfg.port,
        cfg.interval_seconds,
    )
    run_loop(collector, collect, cfg.interval_seconds, stop)
