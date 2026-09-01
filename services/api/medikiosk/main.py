"""FastAPI modular monolith entrypoint (CLAUDE.md §47).

One deployed backend serving four role-aware surfaces (§4). The application is a
monolith by deliberate choice — Kubernetes and service decomposition are
[FUTURE] until real multi-tenant load proves the need (§42–45).
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from medikiosk.config import Settings, get_settings
from medikiosk.context import AppContext
from medikiosk.errors import MediKioskError, ValidationFailed
from medikiosk.observability.logging_setup import configure_logging, get_logger
from medikiosk.routers import (
    admin,
    audit,
    consent,
    documents,
    governance,
    health,
    kiosk,
    physician,
    security_console,
    session as session_router,
    triage,
    upload,
    voice,
)

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings)
    ctx = await AppContext.create(settings, connect_db=app.state.connect_db)
    app.state.ctx = ctx
    try:
        yield
    finally:
        await ctx.aclose()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation id + PHI-redacted access log (§28, §39).

    The access log records the route TEMPLATE, never the raw path: a path
    contains ids, and an id is an identifier. Everything emitted here goes
    through the redaction processor like any other log line.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            log.exception(
                "request_failed",
                http_method=request.method,
                http_route=_route_template(request),
                duration_ms=duration_ms,
            )
            raise
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = request_id
        log.info(
            "request_completed",
            http_method=request.method,
            http_route=_route_template(request),
            http_status=response.status_code,
            duration_ms=duration_ms,
            client_kind=_client_kind(request),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Secure headers on every frontend-facing response (§27)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Cross-Origin-Opener-Policy", "same-origin"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            # The kiosk needs camera and microphone; nothing else is granted.
            "camera=(self), microphone=(self), geolocation=(), payment=()",
        )
        # A clinical record must never be cached by an intermediary.
        if request.url.path.startswith("/v1"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        return response


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


def _client_kind(request: Request) -> str:
    path = request.url.path
    if path.startswith("/v1/kiosk") or path.startswith("/v1/sessions"):
        return "kiosk"
    if path.startswith("/v1/upload"):
        return "phone_upload"
    if path.startswith("/internal"):
        return "internal"
    return "staff"


def create_app(settings: Settings | None = None, *, connect_db: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="MediKiosk API",
        version="0.1.0",
        description=(
            "Hospital kiosk-first patient case-taking platform. "
            "AI is assistive only; deterministic clinical logic and physician "
            "authority are never bypassed (CLAUDE.md §10, §19, §21)."
        ),
        lifespan=lifespan,
        root_path=settings.api_root_path,
        docs_url="/docs" if settings.is_synthetic_data_environment else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_synthetic_data_environment else None,
    )
    app.state.settings = settings
    app.state.connect_db = connect_db

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_credentials=False,  # bearer tokens only; no cookie surface, no CSRF vector
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-request-id", "idempotency-key"],
        max_age=600,
    )

    @app.exception_handler(MediKioskError)
    async def _handle_domain_error(_request: Request, exc: MediKioskError):
        """Typed error → stable reason_code.

        The body never carries clinical content or an internal message: the
        frontend maps ``reason_code`` to a localized, patient-appropriate string
        (§28, §37).
        """
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"reason_code": exc.reason_code, "detail": exc.detail}},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception):
        log.error("unhandled_exception", error_class=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": {"reason_code": "internal_error", "detail": {}}},
        )

    for router in (
        health.router,
        kiosk.router,
        consent.router,
        session_router.router,
        voice.router,
        upload.router,
        triage.router,
        physician.router,
        documents.router,
        governance.router,
        admin.router,
        audit.router,
        security_console.router,
    ):
        app.include_router(router)

    from pathlib import Path
    from fastapi.responses import HTMLResponse

    @app.get("/kiosk", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    async def serve_kiosk():
        """Serve the interactive Voice Kiosk UI (§18, §54)."""
        kiosk_path = Path(__file__).parent / "static" / "kiosk.html"
        if kiosk_path.exists():
            return HTMLResponse(content=kiosk_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>MediKiosk API</h1><p>Visit /docs for API documentation</p>")

    @app.post("/v1/sessions/dev/quick-start")
    async def dev_quick_start(request: Request):
        """Helper for dev/testing to mint a live session with first question in 1 click."""
        data = {}
        try:
            data = await request.json()
        except Exception:
            pass
        patient_name = data.get("full_name", "Ramesh Kumar (Demo Patient)")
        language = data.get("language", "en")
        dept_code = data.get("department_code", "GEN-MED")
        respondent_type = data.get("respondent_type", "patient")
        caregiver_name = data.get("caregiver_name")
        caregiver_relationship = data.get("caregiver_relationship")
        caregiver_ack_method = data.get("caregiver_ack_method")

        ctx: AppContext = request.app.state.ctx
        tenant_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        
        from medikiosk.db import Principal
        from medikiosk.modules.identity import service as identity
        from medikiosk.modules.consent.service import Purpose, ConsentGrant, record_consents
        from medikiosk.modules.caregiver import service as caregiver_service
        from medikiosk.modules.session import service as session_service

        principal = Principal(tenant_id=tenant_id, role="kiosk_device")

        async with ctx.db.transaction(principal) as conn:
            # 1. Get or create department
            dept_row = await conn.fetchrow(
                "SELECT id, code, protocol_family FROM department WHERE code = $1 OR code = 'GEN-MED' LIMIT 1",
                dept_code
            )
            dept_id = dept_row["id"]
            family = dept_row["protocol_family"]

            # 2. Register local patient
            demo_hosp_id = f"HOSP-DEMO-{uuid.uuid4().hex[:6]}"
            patient = await identity.register_local(
                conn,
                principal,
                hospital_local_id=demo_hosp_id,
                full_name=patient_name,
                year_of_birth=1982,
                gender="male",
                phone_last4="1234",
                preferred_language=language,
            )

            caregiver_auth_id = None
            if respondent_type == "caregiver":
                if not caregiver_name or not caregiver_relationship or caregiver_ack_method not in ("voice", "touch"):
                    raise ValidationFailed(
                        "caregiver details and patient acknowledgment are required",
                        reason_code="caregiver_ack_required",
                    )
                authorization = await caregiver_service.record_patient_acknowledgment(
                    conn,
                    principal,
                    patient_id=patient.id,
                    caregiver_name=caregiver_name,
                    relationship=caregiver_relationship,
                    ack_method=caregiver_ack_method,
                )
                caregiver_auth_id = authorization.id

            # 3. Grant staff access, voice capture, and AI processing consent
            await record_consents(
                conn,
                principal,
                patient_id=patient.id,
                grants=[
                    ConsentGrant(purpose=Purpose.STAFF_ACCESS, granted=True),
                    ConsentGrant(purpose=Purpose.VOICE_CAPTURE, granted=True),
                    ConsentGrant(purpose=Purpose.AI_PROCESSING, granted=True),
                ],
                notice_version="2025.1",
                notice_language=language,
                audio_explained=True,
                grantor_type="patient",
            )

            # 4. Create interview session
            session_snap = await session_service.create_session(
                conn,
                principal,
                patient_id=patient.id,
                department_id=dept_id,
                device_id=None,
                protocol_family=family,
                protocol_version="v1",
                language=language,
                respondent_type=respondent_type,
                caregiver_auth_id=caregiver_auth_id,
            )

        token, _ = ctx.tokens.mint(
            "session",
            tenant_id=tenant_id,
            ttl_seconds=3600,
            session_id=session_snap.id,
            patient_id=patient.id,
            department_id=dept_id,
            subject_role="caregiver_respondent" if respondent_type == "caregiver" else "patient",
            actor_id=patient.id,
        )

        return {
            "session_id": str(session_snap.id),
            "session_token": token,
            "patient_id": str(patient.id),
            "department_id": str(dept_id),
            "language": language,
            "protocol_family": family,
            "first_question": {
                "field_id": "gm.cc.primary_complaint",
                "category": "Primary Complaint (SOCRATES)",
                "question_text": "What is the main problem bringing you to the hospital today?",
                "hint": "Speak your main symptoms (e.g., chest pain, fever, headache, breathing trouble)"
            }
        }

    return app


app = create_app()
