import threading

from pgsentry.exporter import SnapshotCollector, run_loop
from pgsentry.models import CheckResult, Sample, Status


def metrics(collector: SnapshotCollector) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Flatten the collector output to {(name, sorted labels): value}."""
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in collector.collect():
        for s in family.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def result(name: str, status: Status, *samples: Sample) -> CheckResult:
    return CheckResult(name, status, "msg", samples)


def test_check_status_and_samples_become_gauges() -> None:
    collector = SnapshotCollector()
    collector.update(
        [
            result(
                "replication",
                Status.CRIT,
                Sample("pgsentry_replication_lag_bytes", 42.0, {"replica": "r1"}),
            ),
            result("role", Status.OK),
        ],
        duration=0.25,
    )
    m = metrics(collector)
    assert m[("pgsentry_check_status", (("check", "replication"),))] == 2
    assert m[("pgsentry_check_status", (("check", "role"),))] == 0
    assert m[("pgsentry_replication_lag_bytes", (("replica", "r1"),))] == 42
    assert m[("pgsentry_last_run_duration_seconds", ())] == 0.25
    assert m[("pgsentry_last_run_timestamp_seconds", ())] > 0


def test_a_replica_that_disappears_disappears_from_the_metrics() -> None:
    collector = SnapshotCollector()
    lag = "pgsentry_replication_lag_bytes"
    collector.update(
        [
            result(
                "replication",
                Status.OK,
                Sample(lag, 1.0, {"replica": "r1"}),
                Sample(lag, 2.0, {"replica": "r2"}),
            )
        ],
        0,
    )
    assert (lag, (("replica", "r2"),)) in metrics(collector)
    collector.update([result("replication", Status.OK, Sample(lag, 1.0, {"replica": "r1"}))], 0)
    after = metrics(collector)
    assert (lag, (("replica", "r1"),)) in after
    assert (lag, (("replica", "r2"),)) not in after


def test_before_the_first_cycle_nothing_but_meta_metrics_exist() -> None:
    m = metrics(SnapshotCollector())
    assert m[("pgsentry_last_run_timestamp_seconds", ())] == 0
    assert not any(name == "pgsentry_check_status" for name, _ in m)


def test_loop_survives_a_failing_cycle() -> None:
    collector = SnapshotCollector()
    stop = threading.Event()
    calls: list[int] = []

    def flaky() -> list[CheckResult]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()
        return [result("role", Status.OK)]

    run_loop(collector, flaky, interval=0.01, stop=stop)
    assert len(calls) == 2
    assert ("pgsentry_check_status", (("check", "role"),)) in metrics(collector)
