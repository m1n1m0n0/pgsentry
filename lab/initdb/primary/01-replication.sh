#!/bin/bash
# Runs once, when the primary's data directory is first created (docker-entrypoint-initdb.d).
set -e

# 1. A role the standby uses to stream WAL, and a physical slot reserved for it so the
#    primary keeps WAL until the standby has consumed it.
psql -v ON_ERROR_STOP=1 -v repl_password="$REPLICATION_PASSWORD" \
     --username "$POSTGRES_USER" --dbname postgres <<'SQL'
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'repl_password';
SELECT pg_create_physical_replication_slot('replica1_slot');
SQL

# 2. The default pg_hba.conf does not let "all" match replication connections, so
#    allow the replicator explicitly.
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"

# 3. The least-privilege monitoring role, created from the same SQL file the README tells
#    real users to run.
psql -v ON_ERROR_STOP=1 -v pgsentry_password="$PGSENTRY_PASSWORD" \
     --username "$POSTGRES_USER" --dbname postgres -f /sql/create_monitor_role.sql

# 4. Something to write to, so there is WAL to replicate.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
CREATE TABLE lab_events (
    id         bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    payload    text NOT NULL
);
SQL
