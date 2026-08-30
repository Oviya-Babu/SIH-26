#!/usr/bin/env bash
# =============================================================================
# Local PostgreSQL for development WITHOUT Docker.
#
# The canonical development stack is Docker Compose (CLAUDE.md §55). This script
# exists for one reason: the RLS guarantees of §30 are the foundation everything
# else rests on, and they must be verifiable on any machine, including one where
# the container runtime is unavailable. It runs a self-contained PostgreSQL under
# $HOME with no root privileges.
#
#   scripts/local_pg.sh start    # initdb (first run) + start on PGPORT
#   scripts/local_pg.sh stop
#   scripts/local_pg.sh status
#   scripts/local_pg.sh reset     # destroy and recreate the cluster
#   scripts/local_pg.sh psql -c '...'
# =============================================================================
set -euo pipefail

PG_HOME="${PG_HOME:-$HOME/.local/opt/pgsql}"
PGDATA="${PGDATA:-$HOME/.local/var/medikiosk-pgdata}"
PGPORT="${PGPORT:-55432}"
PG_SOCKET_DIR="${PG_SOCKET_DIR:-$HOME/.local/var/medikiosk-pgsock}"
PG_LOG="${PG_LOG:-$HOME/.local/var/medikiosk-pg.log}"
OWNER_USER="${OWNER_USER:-medikiosk_owner}"
OWNER_PASSWORD="${OWNER_PASSWORD:-devonly_change_me}"
DB_NAME="${DB_NAME:-medikiosk}"

if [[ ! -x "$PG_HOME/bin/postgres" ]]; then
    echo "PostgreSQL binaries not found at $PG_HOME" >&2
    echo "Use the Docker Compose stack (infra/docker/docker-compose.yml) instead." >&2
    exit 1
fi

export PATH="$PG_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$PG_HOME/lib:${LD_LIBRARY_PATH:-}"

mkdir -p "$(dirname "$PGDATA")" "$PG_SOCKET_DIR"

cmd_init() {
    if [[ -f "$PGDATA/PG_VERSION" ]]; then
        echo "cluster already initialised at $PGDATA"
        return 0
    fi
    echo "initialising cluster at $PGDATA"
    local pwfile
    pwfile="$(mktemp)"
    printf '%s' "$OWNER_PASSWORD" > "$pwfile"
    initdb -D "$PGDATA" \
        --username="$OWNER_USER" \
        --pwfile="$pwfile" \
        --auth-local=trust \
        --auth-host=scram-sha-256 \
        --encoding=UTF8 \
        --locale=C \
        --data-checksums >/dev/null
    rm -f "$pwfile"

    # Listen on loopback only: this is a development cluster and must not be
    # reachable from the network.
    cat >> "$PGDATA/postgresql.conf" <<EOF

# --- MediKiosk local development ---
listen_addresses = '127.0.0.1'
port = $PGPORT
unix_socket_directories = '$PG_SOCKET_DIR'
log_statement = 'ddl'
log_min_duration_statement = 500
log_line_prefix = '%m [%p] %u@%d '
max_connections = 100
shared_buffers = 128MB
EOF
}

cmd_start() {
    cmd_init
    if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
        echo "already running on port $PGPORT"
    else
        pg_ctl -D "$PGDATA" -l "$PG_LOG" -w -o "-p $PGPORT" start
    fi
    cmd_ensure_db
    echo
    echo "owner DSN : postgresql://$OWNER_USER:$OWNER_PASSWORD@127.0.0.1:$PGPORT/$DB_NAME"
    echo "app DSN   : postgresql://medikiosk_app:medikiosk_app@127.0.0.1:$PGPORT/$DB_NAME"
    echo "            (the app role is created by migration 0001 and is NOBYPASSRLS)"
}

cmd_ensure_db() {
    # The embedded build ships server binaries only (no psql/createdb), so the
    # database is created through the project's own Python client.
    PGPORT="$PGPORT" OWNER_USER="$OWNER_USER" OWNER_PASSWORD="$OWNER_PASSWORD" \
        DB_NAME="$DB_NAME" \
        "$(dirname "$0")/../services/api/.venv/bin/python" - <<'PY'
import asyncio, os
import asyncpg

async def main():
    dsn = (
        f"postgresql://{os.environ['OWNER_USER']}:{os.environ['OWNER_PASSWORD']}"
        f"@127.0.0.1:{os.environ['PGPORT']}/postgres"
    )
    conn = await asyncpg.connect(dsn)
    try:
        name = os.environ["DB_NAME"]
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if exists:
            print(f"database {name} already present")
        else:
            await conn.execute(f'CREATE DATABASE "{name}"')
            print(f"created database {name}")
    finally:
        await conn.close()

asyncio.run(main())
PY
}

cmd_stop() {
    pg_ctl -D "$PGDATA" -m fast stop || echo "not running"
}

cmd_status() {
    pg_ctl -D "$PGDATA" status || true
}

cmd_reset() {
    cmd_stop || true
    rm -rf "$PGDATA"
    echo "cluster destroyed"
    cmd_start
}

case "${1:-start}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    reset)  cmd_reset ;;
    init)   cmd_init ;;
    psql)   echo "the embedded build ships no psql; use the API venv + asyncpg" >&2; exit 2 ;;
    *)      echo "usage: $0 {start|stop|status|reset|init|psql}" >&2; exit 2 ;;
esac
