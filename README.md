# pgsentry

A Python CLI and small service that checks the health of a PostgreSQL primary/replica
pair and exposes the result as a table, JSON, an exit code and Prometheus metrics.

**Status:** database health checks, the Prometheus exporter, a local lab, alert rules and a
Grafana dashboard are done. The Kubernetes pod audit and Trivy summary, deployment
manifests and full CI are not built yet (see [Limitations](#limitations)).

The reasoning behind what is and is not checked, the threat model and the exit codes is in
[docs/DESIGN.md](docs/DESIGN.md).

## What it checks

| Check | Runs on | Source | Why it matters |
|---|---|---|---|
| `role` | both | `pg_is_in_recovery()` | after a failover the node you think is primary may not be |
| `replication` | primary | `pg_stat_replication` | standbys missing or behind (bytes and seconds) |
| `wal_receiver` | standby | `pg_stat_wal_receiver`, `pg_last_wal_*` | the standby's own view: receiver gone, replay paused, received-but-unapplied WAL |
| `slots` | both | `pg_replication_slots` | a slot nobody reads keeps WAL until the disk fills |
| `connections` | both | `pg_stat_activity` | at 100% new connections are refused |
| `long_transactions` | both | `pg_stat_activity` | blocks VACUUM and migrations |
| `backup` | either | marker file or S3 object | a replica is not a backup |

Each check returns OK, WARN or CRIT using thresholds from the config. Why both
`pg_stat_replication` and `pg_stat_wal_receiver`? The primary cannot see a standby that has
lost its connection (the row just disappears) or one whose replay is paused, so the standby
reports on itself as well.

## Quick start (local lab)

Needs Docker and Python 3.11+.

```
pip install -e ".[dev]"
make lab-up            # primary, replica, two pgsentry exporters, Prometheus, Grafana
make lab-backup        # records a "successful backup" so the backup check is green
make lab-check         # runs `pgsentry db check` against primary and replica
```

(No `make`? Each target is one `docker compose -f lab/docker-compose.yml ...` command; see the
[Makefile](Makefile).) Grafana is on http://127.0.0.1:3000 (admin / `lab-only-password`),
Prometheus on http://127.0.0.1:9090. Everything is bound to 127.0.0.1 and uses a throwaway
password; the lab is not a deployment example.

### Example report

```
$ PGSENTRY_DB_PASSWORD=lab-only-password pgsentry -c lab/pgsentry-primary.yaml db check
PostgreSQL 127.0.0.1:55432
┌───────────────────┬────────┬────────────────────────────────────────────────┐
│ Check             │ Status │ Message                                        │
├───────────────────┼────────┼────────────────────────────────────────────────┤
│ connection        │ OK     │ connected to 127.0.0.1:55432/postgres as       │
│                   │        │ pgsentry                                       │
│ role              │ OK     │ primary (PostgreSQL 16.6)                      │
│ replication       │ OK     │ 1 replica(s) streaming, max replay lag 0 B     │
│ slots             │ OK     │ 1 slot(s), all active, WAL retention ok        │
│ connections       │ OK     │ 2 of 97 usable connections in use (2%)         │
│ long_transactions │ OK     │ no open transactions                           │
│ backup            │ OK     │ last successful backup 4.6s ago                │
└───────────────────┴────────┴────────────────────────────────────────────────┘
Overall: OK
```

`--format json` prints the same data for scripts. Exit code: `0` OK, `2` CRIT, `1` WARN only
with `--fail-on warn`, `3` pgsentry could not run (bad config or command line).

### Verify replication by hand

pgsentry is only useful if you can confirm it tells the truth. In the lab:

```
# 1. Is the standby really a standby?   -> t
docker compose -f lab/docker-compose.yml exec replica psql -U postgres -tc "SELECT pg_is_in_recovery()"

# 2. What does the primary see?   -> one row, state = streaming
docker compose -f lab/docker-compose.yml exec primary psql -U postgres -x -c \
  "SELECT application_name, state, sync_state, sent_lsn, replay_lsn FROM pg_stat_replication"

# 3. Write on the primary, read on the standby
docker compose -f lab/docker-compose.yml exec primary psql -U postgres -c \
  "INSERT INTO lab_events (payload) VALUES ('hello')"
docker compose -f lab/docker-compose.yml exec replica psql -U postgres -tc \
  "SELECT count(*) FROM lab_events WHERE payload = 'hello'"      # -> 1

# 4. The standby refuses writes   -> ERROR: cannot execute INSERT in a read-only transaction
docker compose -f lab/docker-compose.yml exec replica psql -U postgres -c \
  "INSERT INTO lab_events (payload) VALUES ('nope')"
```

### Break it on purpose

| Drill | What you should see |
|---|---|
| `make lab-pause-replay` then `make lab-load` | `wal_receiver` WARN "WAL replay is paused; 67.0 MiB received but not replayed"; `replication` on the primary shows the same lag in bytes, then CRIT once PG's time estimate passes 300 s; `PgsentryReplicaReplayPaused` and `PgsentryReplicationLagHigh` go pending, then firing. `make lab-resume-replay` clears them. |
| `make lab-stop-replica` | `replication` CRIT "only 0 of 1 expected replicas streaming", `slots` WARN "slot 'replica1_slot' is inactive", exit code 2; `PgsentryReplicaDown` and `PgsentryCannotReachDatabase` fire within about a minute. `make lab-start-replica` undoes it. |
| delete `lab/state/last_backup` | `backup` CRIT "marker file ... not found" |

## Prometheus metrics

`pgsentry serve` runs the checks every `serve.interval_seconds` and serves the last result
at `/metrics`. A scrape never queries the database. Default port 9188, bound to 127.0.0.1
unless `serve.bind` says otherwise.

| Metric | Labels | Meaning |
|---|---|---|
| `pgsentry_up` | | 1 if the database could be queried |
| `pgsentry_check_status` | `check` | 0 OK, 1 WARN, 2 CRIT |
| `pgsentry_is_replica` | | 1 if in recovery |
| `pgsentry_replication_streaming_replicas` | | standbys in state `streaming` (primary) |
| `pgsentry_replication_expected_replicas` | | `postgres.expected_replicas`, for alert rules |
| `pgsentry_replication_lag_bytes` | `replica` | WAL bytes not yet replayed, primary's view |
| `pgsentry_replication_lag_seconds` | `replica` | PG's replay-lag estimate (absent while idle) |
| `pgsentry_replica_wal_receiver_streaming` | | 1 if the standby's receiver is streaming |
| `pgsentry_replica_replay_lag_bytes` | | received but not replayed, standby's view |
| `pgsentry_replica_replay_lag_seconds` | | replay delay (absent until a commit is replayed) |
| `pgsentry_replica_replay_paused` | | 1 if replay is paused |
| `pgsentry_replication_slot_retained_bytes` | `slot` | WAL held back by the slot |
| `pgsentry_replication_slot_active` | `slot` | 1 if a consumer is attached |
| `pgsentry_connections_used` / `_max` / `_used_ratio` | | backends in use / usable / ratio |
| `pgsentry_longest_transaction_seconds` | | age of the oldest open transaction |
| `pgsentry_backup_age_seconds` | | seconds since the last recorded good backup |
| `pgsentry_last_run_timestamp_seconds` / `_duration_seconds` | | when the last cycle ended / how long it took |

Alert rules (replication lag, replica down, WAL receiver down, replay paused, slot retaining
WAL, backup too old, connections, long transaction, exporter down or stale) are in
[deploy/monitoring/alerts.yml](deploy/monitoring/alerts.yml); the Grafana dashboard is
[deploy/monitoring/grafana-dashboard.json](deploy/monitoring/grafana-dashboard.json). The
numeric thresholds in the rules mirror the defaults in the config, so change both together.

## Using it on your own database

1. Create the monitoring role on the primary: [sql/create_monitor_role.sql](sql/create_monitor_role.sql).
   It grants `pg_monitor` only, limits the role to 3 connections, and makes its sessions
   read-only with a 5 s statement timeout.
2. Copy [config.example.yaml](config.example.yaml) and edit it.
3. Provide the password as `PGSENTRY_DB_PASSWORD` or `PGSENTRY_DB_PASSWORD_FILE`. A
   `password` key in the YAML is rejected on purpose.
4. `pgsentry -c config.yaml db check` (one instance per process; run one per node), or
   `pgsentry -c config.yaml serve`. `pgsentry config` prints the effective settings.

`docker build -t pgsentry .` makes a multi-stage image that runs as UID 10001.

## Development

```
make check     # ruff, mypy --strict, pytest (70 tests, no database needed)
```

Tests feed the check functions canned rows through a small `Querier` interface; the SQL
itself is exercised by the lab.

## Limitations

- **Not built yet:** Kubernetes pod audit, Trivy summary, Kubernetes/Ansible deployment,
  image-build and manifest checks in CI. The only workflow is lint, types, tests and a
  gitleaks scan.
- One instance per process. Two nodes need two processes (the lab runs two exporters).
- Physical streaming replication only. No logical replication, Patroni or operator state,
  and no WAL-archive check.
- The backup check trusts a marker; it does not restore or verify anything.
- `/metrics` has no authentication or TLS. Restrict it with network policy or a reverse proxy.
- Alert thresholds are duplicated between the pgsentry config and `alerts.yml`.
- Postgres's own `replay_lag` estimate is stale after a long pause and decays slowly, so a
  standby can stay WARN or CRIT on time for a while after the byte lag is back to zero.
- Tested against PostgreSQL 16 only.
- No transaction ID wraparound check yet (`age(datfrozenxid)`), which is the obvious next one.

MIT licensed.
