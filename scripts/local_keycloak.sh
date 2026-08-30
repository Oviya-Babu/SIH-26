#!/usr/bin/env bash
# =============================================================================
# Local Keycloak for development WITHOUT Docker.
#
# The canonical stack runs Keycloak as a container (CLAUDE.md §55). This script
# exists so the OIDC half of the §5.1 auth chain — and therefore the Phase 0 and
# Phase 2 Definitions of Done — can be proven on a machine with no container
# runtime. It runs a self-contained Keycloak under $HOME with no root privileges.
#
# The realm is templated: SEED_TENANT_ID / SEED_CONTROL_TENANT_ID are replaced
# with the real tenant uuids created by scripts/seed_demo.py, so the tenant claim
# in a token matches an actual tenant row. Without that substitution every token
# would carry a tenant that does not exist, and RLS would correctly show nothing.
#
#   scripts/local_keycloak.sh start
#   scripts/local_keycloak.sh stop
#   scripts/local_keycloak.sh token physician.genmed 'Physician!GenMed2026'
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KC_HOME="${KC_HOME:-$HOME/.local/opt/keycloak}"
JAVA_HOME_DIR="${JAVA_HOME_DIR:-$HOME/.local/opt/jdk}"
KC_PORT="${KC_PORT:-8080}"
KC_LOG="${KC_LOG:-$HOME/.local/var/medikiosk-keycloak.log}"
KC_PID="${KC_PID:-$HOME/.local/var/medikiosk-keycloak.pid}"
KC_ADMIN="${KC_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:-devonly_change_me}"
REALM_TEMPLATE="$REPO_ROOT/infra/keycloak/realm-medikiosk.json"
REALM_RENDERED="$HOME/.local/var/medikiosk-realm-rendered.json"
OWNER_DSN="${MEDIKIOSK_MIGRATION_DSN:-postgresql://medikiosk_owner:devonly_change_me@127.0.0.1:55432/medikiosk}"

if [[ ! -x "$JAVA_HOME_DIR/bin/java" ]]; then
    echo "JDK not found at $JAVA_HOME_DIR" >&2
    exit 1
fi
if [[ ! -x "$KC_HOME/bin/kc.sh" ]]; then
    echo "Keycloak not found at $KC_HOME" >&2
    exit 1
fi

export JAVA_HOME="$JAVA_HOME_DIR"
export PATH="$JAVA_HOME/bin:$PATH"
mkdir -p "$(dirname "$KC_LOG")"

render_realm() {
    # Pull the real tenant uuids out of the database so the tenant_id claim in a
    # token refers to a tenant that actually exists.
    MEDIKIOSK_MIGRATION_DSN="$OWNER_DSN" \
    REALM_TEMPLATE="$REALM_TEMPLATE" REALM_RENDERED="$REALM_RENDERED" \
        "$REPO_ROOT/services/api/.venv/bin/python" - <<'PY'
import asyncio, json, os, sys
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["MEDIKIOSK_MIGRATION_DSN"])
    try:
        rows = {
            r["slug"]: str(r["id"])
            for r in await conn.fetch("SELECT slug, id FROM tenant")
        }
    finally:
        await conn.close()

    primary = rows.get("sih-demo-hospital")
    control = rows.get("isolation-control-hospital")
    if not primary:
        print("no seeded tenant found; run scripts/seed_demo.py first", file=sys.stderr)
        sys.exit(1)

    raw = open(os.environ["REALM_TEMPLATE"], encoding="utf-8").read()
    raw = raw.replace("SEED_TENANT_ID", primary)
    raw = raw.replace("SEED_CONTROL_TENANT_ID", control or primary)

    document = json.loads(raw)
    # Strip the documentation-only keys Keycloak's importer rejects.
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if not k.startswith("_")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    with open(os.environ["REALM_RENDERED"], "w", encoding="utf-8") as handle:
        json.dump(strip(document), handle, indent=2)
    print(f"realm rendered: tenant={primary} control={control}")

asyncio.run(main())
PY
}

cmd_start() {
    if [[ -f "$KC_PID" ]] && kill -0 "$(cat "$KC_PID")" 2>/dev/null; then
        echo "already running (pid $(cat "$KC_PID"))"
        return 0
    fi
    render_realm
    mkdir -p "$KC_HOME/data/import"
    cp "$REALM_RENDERED" "$KC_HOME/data/import/realm-medikiosk.json"

    echo "starting Keycloak on port $KC_PORT (dev mode, H2 store)"
    KC_BOOTSTRAP_ADMIN_USERNAME="$KC_ADMIN" \
    KC_BOOTSTRAP_ADMIN_PASSWORD="$KC_ADMIN_PASSWORD" \
    KC_HEALTH_ENABLED=true \
    KC_HOSTNAME_STRICT=false \
        nohup "$KC_HOME/bin/kc.sh" start-dev \
            --http-port="$KC_PORT" \
            --import-realm \
            > "$KC_LOG" 2>&1 &
    echo $! > "$KC_PID"

    echo -n "waiting for realm to come up"
    for _ in $(seq 1 90); do
        if curl -fsS "http://127.0.0.1:$KC_PORT/realms/medikiosk/.well-known/openid-configuration" \
                >/dev/null 2>&1; then
            echo " ready"
            echo "issuer: http://127.0.0.1:$KC_PORT/realms/medikiosk"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo " TIMED OUT"
    tail -30 "$KC_LOG" >&2
    return 1
}

cmd_stop() {
    if [[ -f "$KC_PID" ]]; then
        kill "$(cat "$KC_PID")" 2>/dev/null || true
        rm -f "$KC_PID"
        echo "stopped"
    else
        pkill -f "keycloak" 2>/dev/null || echo "not running"
    fi
}

cmd_token() {
    local username="${1:?username required}"
    local password="${2:?password required}"
    curl -fsS -X POST \
        "http://127.0.0.1:$KC_PORT/realms/medikiosk/protocol/openid-connect/token" \
        -d "client_id=medikiosk-staff" \
        -d "grant_type=password" \
        -d "username=$username" \
        -d "password=$password" \
        -d "scope=openid profile email medikiosk-tenant"
}

case "${1:-start}" in
    start) cmd_start ;;
    stop)  cmd_stop ;;
    logs)  tail -f "$KC_LOG" ;;
    token) shift; cmd_token "$@" ;;
    render) render_realm ;;
    *) echo "usage: $0 {start|stop|logs|render|token <user> <password>}" >&2; exit 2 ;;
esac
