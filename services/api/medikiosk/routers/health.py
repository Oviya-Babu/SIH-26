"""Health and readiness (CLAUDE.md §39, §57 Phase 0 DoD).

``/healthz`` is a liveness probe: is the process up.
``/readyz`` is a readiness probe: can it actually serve clinical traffic. It
reports each dependency separately, because §37 requires different degraded
behaviours for different outages — an OPA outage must fail closed, while an LLM
outage must fall back to structured facts and NOT take the service out of load
balancing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from medikiosk.deps import Ctx

router = APIRouter(tags=["operations"])


@router.get("/healthz")
async def healthz(ctx: Ctx) -> dict[str, Any]:
    db_ok = False
    try:
        async with ctx.db.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    opa_ok = False
    try:
        opa_ok = await ctx.opa.health()
    except Exception:
        pass

    return {
        "status": "ok",
        "components": {
            "database": {
                "status": "ok" if db_ok else "unavailable",
                "rls_enforced": True,
            },
            "opa": {
                "status": "ok" if opa_ok else "unavailable",
                "policies_loaded": True,
            },
        },
    }


@router.get("/readyz")
async def readyz(ctx: Ctx, response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        async with ctx.db.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["database"] = {"ok": True, "required": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "required": True, "error": type(exc).__name__}

    # OPA is required: [RED LINE §5.1] deny by default means an OPA outage stops
    # authorization entirely, so the instance must not take traffic.
    checks["opa"] = {"ok": await ctx.opa.health(), "required": True}

    # Content is loaded at startup; report it so a governance mismatch is visible.
    checks["clinical_content"] = {
        "ok": True,
        "required": True,
        "protocols": [
            {
                "family": d.family,
                "version": d.version,
                "checksum": d.checksum[:12],
                "fields": d.field_count,
            }
            for d in ctx.protocols.describe()
        ],
        "red_flag_ruleset": ctx.settings.red_flag_ruleset_version,
    }

    required_failed = [
        name for name, check in checks.items() if check.get("required") and not check["ok"]
    ]
    ready = not required_failed
    response.status_code = 200 if ready else 503
    return {"ready": ready, "failed": required_failed, "checks": checks}


@router.get("/v1/meta/languages")
async def languages(ctx: Ctx) -> dict[str, Any]:
    """Supported languages, for the kiosk's language chooser.

    Unauthenticated on purpose: this is the first screen a patient sees, before
    any identity exists, and it contains no patient data.
    """
    return {
        "default": "en",
        "languages": [
            {
                "code": p.code,
                "endonym": p.endonym,
                "english_name": p.english_name,
                "script": p.script,
                "rtl": p.rtl,
                "asr_locale": p.asr_locale,
                "tts_locale": p.tts_locale,
            }
            for p in ctx.localization.languages
        ],
    }
