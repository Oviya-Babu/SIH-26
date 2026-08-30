# =============================================================================
# MediKiosk API — FastAPI modular monolith (CLAUDE.md §42-45, §47).
#
# Multi-stage: dependencies resolve in a builder, and the runtime image carries
# no build toolchain. The container runs as a non-root user with a read-only
# root filesystem in compose — a container that cannot write to itself cannot be
# persistently modified by an exploit.
# =============================================================================

# --- builder -----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY services/api/pyproject.toml ./
COPY services/api/medikiosk ./medikiosk

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is required by the container healthcheck and nothing else.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 medikiosk \
 && useradd --system --uid 10001 --gid medikiosk --no-create-home medikiosk

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY services/api/medikiosk /srv/medikiosk

# Governed clinical content is mounted READ-ONLY. The running service must never
# be able to alter the protocol or red-flag rules it is executing (§10, §46).
COPY content /srv/content

RUN chown -R medikiosk:medikiosk /srv
USER medikiosk

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# One worker per container; scale by adding containers, not threads. The
# in-process WebSocket alert hub (§50) is per-process, so a multi-worker single
# container would silently deliver alerts to only one worker's subscribers.
CMD ["uvicorn", "medikiosk.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header"]
