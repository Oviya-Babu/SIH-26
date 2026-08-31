#!/bin/bash
# =============================================================================
# MEDIKIOSK PHASE 2 QUICK START — ONE COMMAND PER TERMINAL
#
# This script provides the exact sequence to get Phase 2 fully operational.
# Open 4 terminals and follow the steps below.
# =============================================================================

# =============================================================================
# TERMINAL 1: Start Docker Infrastructure (required first)
# =============================================================================
# cd /home/aghila/SIH-26
# ./scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio clamav otel-collector prometheus grafana
#
# Then wait ~30 seconds for services to be ready. Check:
# ./scripts/compose.sh ps
#
# All services should show "Up". If any show "Exited", check logs:
# ./scripts/compose.sh logs postgres
# ./scripts/compose.sh logs keycloak

# =============================================================================
# TERMINAL 2: Run Migrations (MUST WAIT for Terminal 1 to be ready)
# =============================================================================
# cd /home/aghila/SIH-26/services/api
# source .venv/bin/activate
# cd ../../
# export MEDIKIOSK_MIGRATION_DSN='postgresql://medikiosk_owner:dev_123098_$%_PostGRE_only_change_me@127.0.0.1:5432/medikiosk'
# python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
#
# Output should show:
#   0001_foundation.sql ... ok
#   0002_consent_session.sql ... ok
#   ... (5 migrations total)
#   5 migration(s) applied
#
# Then seed demo data:
# python3 scripts/seed_demo.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
#
# Output will include device credentials — SAVE THESE for smoke test later:
#   tenant=sih-demo-hospital
#   devices: [
#     {label: KIOSK-GENMED-01, credential: abc123...}
#     ...
#   ]

# =============================================================================
# TERMINAL 3: Start Backend API
# =============================================================================
# cd /home/aghila/SIH-26/services/api
# source .venv/bin/activate
# uvicorn medikiosk.main:app --host 0.0.0.0 --port 8000 --reload
#
# Should show:
#   Uvicorn running on http://0.0.0.0:8000
#   Application startup complete

# =============================================================================
# TERMINAL 4: Verify Everything Works
# =============================================================================
# cd /home/aghila/SIH-26
#
# Step 1: Health checks
# curl http://localhost:8000/healthz
# # Should return: {"status":"ok"}
#
# curl http://localhost:8000/readyz
# # Should return: {"ready":true, ...}
#
# Step 2: Languages endpoint
# curl http://localhost:8000/v1/meta/languages
# # Should return 5 languages: en, hi, ta, te, ml
#
# Step 3: Run smoke test (Phase 2 end-to-end validation)
# export MEDIKIOSK_MIGRATION_DSN='postgresql://medikiosk_owner:dev_123098_$%_PostGRE_only_change_me@127.0.0.1:5432/medikiosk'
# DEVICE_CREDENTIAL=$(python3 - <<'PYTHON'
# import asyncio, asyncpg, os
# async def get_cred():
#     conn = await asyncpg.connect(os.environ["MEDIKIOSK_MIGRATION_DSN"])
#     cred = await conn.fetchval("SELECT credential_hash FROM device LIMIT 1")
#     await conn.close()
#     return cred
# print(asyncio.run(get_cred()))
# PYTHON
# )
# python3 scripts/smoke_vertical_slice.py \
#     --base-url http://127.0.0.1:8000 \
#     --device-credential "$DEVICE_CREDENTIAL" \
#     --language en
#
# Output should show:
#   [PASS] service reports ready
#   [PASS] both protocol families loaded: general_medicine, ayush_ayurveda
#   [PASS] exactly five languages: ['en', 'hi', 'ta', 'te', 'ml']
#   [PASS] unprovisioned device refused (404)
#   [PASS] provisioned device accepted (200)
#   ... (many more PASS lines)
#   [PASS] Interview continues → completeness rising
#   [PASS] Respondent attribution visible

# =============================================================================
# STOP EVERYTHING (when done)
# =============================================================================
# Terminal 1: Press Ctrl+C on the docker compose logs, or:
# ./scripts/compose.sh down -v
#
# Terminal 3: Press Ctrl+C to stop uvicorn
#
# Terminal 2 & 4: Just close

# =============================================================================
# TROUBLESHOOTING
# =============================================================================
#
# "Connection refused" on asyncpg:
#   → PostgreSQL not running. Start Terminal 1 first and wait 30s.
#
# "Keycloak health check fails" or "Cannot connect to Keycloak":
#   → Keycloak takes 30-40s to start. Check:
#     ./scripts/compose.sh logs keycloak | tail -20
#
# "Database already exists" on migrate.py:
#   → Migrations are idempotent. Safe to re-run:
#     python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN" --dry-run
#
# "pytest not found" when running tests:
#   → Activate venv first:
#     cd services/api && source .venv/bin/activate && cd ../../
#     python3 -m pytest tests/unit tests/red_flag_regression
#
# "RLS policy mismatch" error:
#   → Check migration status:
#     python3 scripts/migrate.py --status --dsn "$MEDIKIOSK_MIGRATION_DSN"
#   → If migrations look OK, check PostgreSQL RLS setup:
#     docker exec medikiosk-postgres psql -U medikiosk_owner -d medikiosk -c "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public';"

echo "=== PHASE 2 STARTUP GUIDE PRINTED ==="
echo "Read the comments above and open 4 terminals."
echo "Each terminal has its own set of commands."
