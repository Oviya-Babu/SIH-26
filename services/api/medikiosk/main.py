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
from medikiosk.errors import MediKioskError
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

    return app


app = create_app()
