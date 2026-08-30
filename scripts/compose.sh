#!/usr/bin/env bash
# =============================================================================
# Docker Compose wrapper (CLAUDE.md §55).
#
# Exists because of one easy, silent mistake: Compose resolves `.env` relative to
# the COMPOSE FILE's directory, not the working directory. With the compose file
# at infra/docker/, a root-level `.env` is ignored entirely — every
# `${VAR:-default}` quietly falls back to its default, and you get a stack that
# starts cleanly with the wrong credentials.
#
# This wrapper pins --env-file and --project-directory so that cannot happen.
#
#   scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio
#   scripts/compose.sh ps
#   scripts/compose.sh logs -f api
#   scripts/compose.sh down -v
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker/docker-compose.yml"
ENV_FILE="${MEDIKIOSK_ENV_FILE:-$REPO_ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "no env file at $ENV_FILE — copy .env.example to .env first" >&2
    exit 1
fi

exec docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    --project-directory "$REPO_ROOT/infra/docker" \
    "$@"
