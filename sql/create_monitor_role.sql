-- Least-privilege role for pgsentry.
--
-- Run once, as a superuser, on the PRIMARY. Roles are cluster-wide and travel to the
-- standbys through WAL, so there is nothing to run on the replicas.
--
--   psql -h <primary> -U postgres -v pgsentry_password="$(cat /path/to/secret)" \
--        -f sql/create_monitor_role.sql
--
-- Prefer `\password pgsentry` inside psql over -v if statements are logged
-- (log_statement = 'all' would write the password into the server log).

CREATE ROLE pgsentry WITH LOGIN PASSWORD :'pgsentry_password' CONNECTION LIMIT 3;

-- pg_monitor = pg_read_all_settings + pg_read_all_stats + pg_stat_scan_tables.
-- That is enough to read pg_stat_replication, pg_stat_wal_receiver,
-- pg_replication_slots and every session in pg_stat_activity. It grants no access to
-- table data and no ability to write.
GRANT pg_monitor TO pgsentry;

-- Belt and braces: even if someone later grants this role more, its sessions start
-- read-only and cannot run for long.
ALTER ROLE pgsentry SET default_transaction_read_only = on;
ALTER ROLE pgsentry SET statement_timeout = '5s';

-- Recommended in pg_hba.conf: only the monitoring host, only over TLS, e.g.
--   hostssl  postgres  pgsentry  10.0.0.0/24  scram-sha-256
