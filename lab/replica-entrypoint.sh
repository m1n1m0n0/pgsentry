#!/bin/bash
# Entrypoint for the lab standby. Runs as root inside the container (like the official
# image's entrypoint) and drops to the postgres user with gosu.
set -euo pipefail

PGDATA=/var/lib/postgresql/data

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "replica: empty data directory, cloning the primary with pg_basebackup"
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"

    # -X stream  also stream WAL generated during the copy, so the clone is consistent
    # -S         use the slot the primary created for us
    until PGPASSWORD="$REPLICATION_PASSWORD" gosu postgres pg_basebackup \
            --host=primary --username=replicator --pgdata="$PGDATA" \
            --wal-method=stream --slot=replica1_slot --progress --verbose; do
        echo "replica: primary not ready yet, retrying in 2s"
        sleep 2
    done

    # standby.signal is what turns this server into a standby. The connection settings go
    # in postgresql.auto.conf; application_name is how the primary labels this standby in
    # pg_stat_replication.
    touch "$PGDATA/standby.signal"
    cat >> "$PGDATA/postgresql.auto.conf" <<EOF
primary_conninfo = 'host=primary port=5432 user=replicator password=$REPLICATION_PASSWORD application_name=replica1'
primary_slot_name = 'replica1_slot'
EOF
    chown postgres:postgres "$PGDATA/postgresql.auto.conf"
fi

exec gosu postgres postgres -c listen_addresses='*' -c hot_standby=on
