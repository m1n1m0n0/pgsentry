# pgsentry design note

Written after phases 1-3 were built, so it describes what the code does, not a plan.

## Purpose

One small tool answers two questions about a PostgreSQL deployment on Kubernetes or
Docker: *is the database healthy?* and *are the pods around it configured safely?* It
reports as a CLI table or JSON, as Prometheus metrics, and as an exit code a CI job can
gate on. It reads. It never changes anything.

## What it checks

**Database** (one instance per process; run one per node):

| Check | Source | Looks for |
|---|---|---|
| role | `pg_is_in_recovery()` | primary/replica differs from `expected_role` |
| replication (primary) | `pg_stat_replication` | fewer streaming standbys than `expected_replicas`; replay lag in bytes and seconds |
| wal_receiver (standby) | `pg_stat_wal_receiver`, `pg_last_wal_*` | no receiver; replay paused; received-but-unreplayed WAL |
| slots | `pg_replication_slots` | inactive slots; WAL retained; `lost` / `unreserved` |
| connections | `pg_stat_activity`, settings | share of usable `max_connections` in use |
| long_transactions | `pg_stat_activity` | oldest open transaction, incl. idle-in-transaction |
| backup | marker file or S3 object | age of the last recorded successful backup |

**Platform** (pods in one namespace, or YAML files offline): runs as root or may; privileged;
privilege escalation allowed; writable root filesystem; missing CPU/memory limits;
`:latest` or untagged image; `hostPath` volume. Plus a Trivy summary (HIGH/CRITICAL
counts) per unique image.

## What it deliberately does not check

- **Data.** No table contents, no query text (the long-transaction query selects pid, user,
  state and age only), no per-table statistics.
- **Performance tuning.** No cache hit ratio, bloat, slow-query analysis. Use pg_stat_statements
  tooling for that.
- **Backup validity.** It reads a marker saying a backup *finished*; it does not restore
  anything. A restore drill is a separate job.
- **Logical replication, Patroni/operator state, or WAL archiving.**
- **Cluster-wide security.** No RBAC, NetworkPolicy, admission or node audit; only the pod
  spec fields listed above. `hostNetwork`, capabilities and seccomp are not checked yet.
- **Exploitability.** Trivy counts known CVEs in image layers; it says nothing about whether
  they are reachable. `serve` never runs Trivy (slow, and needs the image registry).

## Threat model

*Who can read the metrics?* `/metrics` is plain HTTP with no authentication. It binds to
127.0.0.1 unless configured otherwise; the Kubernetes manifests bind it to the pod IP and a
NetworkPolicy admits only the Prometheus namespace. Metrics carry replica `application_name`,
slot names and numbers, no addresses, query text or credentials. Anyone who can reach the
port learns the topology and health of the database, which is useful to an attacker timing
an attack, so reachability is restricted by network policy, not by the app.

*What can the DB role see?* `pg_monitor` can read settings, statistics, and **every session's
query text** in `pg_stat_activity`, though pgsentry never selects it. It cannot read table
data or write. The role is also limited to 3 connections, starts every session read-only
(`default_transaction_read_only`) and has a 5 s `statement_timeout`. If the pgsentry process
is compromised, an attacker holds that role: they can read query text, not data.

*What can the pod touch?* A ServiceAccount with `get` and `list` on `pods` in one namespace
and nothing else: no secrets, no write verbs, no other resources, no cluster-wide role. The
pod runs non-root with a read-only root filesystem and no capabilities, and the manifests
are audited by pgsentry itself in CI. The DB password is read from an env var or a mounted
file, never from the YAML (a `password` key makes the config invalid) and never printed.
Error text from boto3 and psycopg is reduced to a type name or first line so ARNs and
connection strings do not reach reports.

*Untrusted input.* Image names from the cluster are passed to `trivy` as a single argv
element (no shell) after a format check, with a timeout. SQL is a fixed set of constants;
nothing user-supplied is interpolated.

## Exit-code contract

| Code | Meaning |
|---|---|
| 0 | Worst result is OK, or WARN with the default `--fail-on crit`, or `--fail-on never` |
| 1 | Worst result is WARN and `--fail-on warn` was given |
| 2 | Worst result is CRIT (unreachable database, replica down, backup too old, privileged pod ...) |
| 3 | pgsentry itself could not run: invalid config, unreadable file, bad command line |

Click's own usage errors exit 2 by default; they are remapped to 3 so that 2 always means
"the thing being checked is critical". An unreachable database is a CRIT result (2), not an
error (3): for a monitor, "cannot connect" is the finding.
