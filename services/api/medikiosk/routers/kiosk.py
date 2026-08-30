"""Kiosk device and identity endpoints (CLAUDE.md §7, §8, §33).

The kiosk flow, in order:

    device auth  →  i18n bundles  →  departments  →  identity  →  consent
                 →  session start (routers/session.py)

§8: tenant and department are fixed BY THE DEVICE. Nothing in a request body can
change them, which is why every endpoint here takes its tenant from the device
token rather than from the payload.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field as PField

from medikiosk.db import system_principal
from medikiosk.deps import Ctx, KioskPrincipal
from medikiosk.errors import ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.identity import service as identity
from medikiosk.modules.localization.registry import BUNDLES
from medikiosk.modules.tenant import service as tenant_service
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/v1/kiosk", tags=["kiosk"])


class DeviceAuthRequest(BaseModel):
    device_credential: str = PField(min_length=32, max_length=512)


class DeviceAuthResponse(BaseModel):
    kiosk_token: str
    expires_in: int
    tenant_name: str
    device_label: str | None = None
    department: dict[str, Any] | None
    supported_languages: list[str]


@router.post("/device/token", response_model=DeviceAuthResponse)
async def device_token(ctx: Ctx, payload: DeviceAuthRequest) -> DeviceAuthResponse:
    """Exchange a provisioned device credential for a short-lived kiosk token.

    §33: a stolen or unprovisioned tablet cannot get past this point. The
    credential is only ever compared as a digest.

    This is the one query that must run before a tenant is known, so it uses a
    deliberately narrow bootstrap principal and immediately narrows to the
    resolved tenant for everything afterwards.
    """
    async with ctx.db.pool.acquire() as conn:
        # Runs through the SECURITY DEFINER device_authenticate() function: the
        # only pre-tenant lookup in the system, scoped to a credential digest and
        # a single row. RLS is untouched (migration 0001).
        binding = await tenant_service.authenticate_device(conn, payload.device_credential)

    principal = system_principal(binding.tenant_id, role="kiosk_device")
    async with ctx.db.transaction(principal) as conn:
        await tenant_service.touch_device(conn, binding.device_id)
        await audit.record(
            conn,
            principal,
            action="device.token_issued",
            entity_type="device",
            entity_id=binding.device_id,
            detail={"department_code": binding.department_code},
        )

    token, claims = ctx.tokens.mint(
        "kiosk",
        tenant_id=binding.tenant_id,
        ttl_seconds=ctx.settings.session_token_ttl_seconds,
        device_id=binding.device_id,
        department_id=binding.department_id,
        subject_role="staff",
    )
    log.info(
        "device_token_issued",
        component="kiosk",
        tenant_id=binding.tenant_id,
        device_id=binding.device_id,
        department_code=binding.department_code,
    )
    return DeviceAuthResponse(
        kiosk_token=token,
        expires_in=claims.expires_at - claims.issued_at,
        tenant_name=binding.tenant_name,
        device_label=None,
        department=(
            {
                "id": str(binding.department_id),
                "code": binding.department_code,
                "display_name": binding.department_name,
                "protocol_family": binding.protocol_family,
            }
            if binding.department_id
            else None
        ),
        supported_languages=[p.code for p in ctx.localization.languages],
    )


@router.get("/i18n/{language}")
async def i18n_bundle(
    ctx: Ctx,
    language: str,
    bundles: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    """Serve localized UI resources to the kiosk.

    The frontend ships NO hardcoded copy: it fetches these bundles, so a
    translation fix is a content change and never a frontend release. Requires
    only a kiosk token's worth of trust — these strings are not patient data —
    but is served under /v1/kiosk so it is covered by the same CORS and header
    policy as the rest of the kiosk surface.
    """
    code = ctx.localization.normalize(language)
    requested = tuple(bundles) if bundles else BUNDLES
    unknown = set(requested) - set(BUNDLES)
    if unknown:
        raise ValidationFailed(
            "unknown bundle(s): " + ", ".join(sorted(unknown)),
            reason_code="unknown_bundle",
        )
    profile = ctx.localization.profile(code)
    return {
        "language": code,
        "profile": {
            "endonym": profile.endonym,
            "script": profile.script,
            "rtl": profile.rtl,
            "asr_locale": profile.asr_locale,
            "tts_locale": profile.tts_locale,
            "tts_voice": profile.tts_voice,
        },
        "bundles": {name: ctx.localization.bundle(code, name) for name in requested},
    }


@router.get("/departments")
async def departments(ctx: Ctx, principal: KioskPrincipal) -> dict[str, Any]:
    """Departments this kiosk may start a session for.

    A device bound to a department offers only that one; an unbound device (a
    shared registration desk) offers the tenant's active list.
    """
    async with ctx.db.readonly(principal) as conn:
        rows = await tenant_service.list_departments(conn)

    available = [
        {
            "id": str(d.id),
            "code": d.code,
            "display_name": d.display_name,
            "protocol_family": d.protocol_family,
        }
        for d in rows
        if principal.department_id is None or d.id == principal.department_id
    ]
    return {"fixed_by_device": principal.department_id is not None, "departments": available}


class IdentifyRequest(BaseModel):
    """Identity at the kiosk (§7.1).

    ``mode`` selects the path. There is deliberately no Aadhaar field: the
    schema itself gives no way to send one, and the service refuses
    Aadhaar-shaped values in the fields that do exist.
    """

    mode: Literal["abha", "local"]
    preferred_language: str = "en"
    full_name: str
    year_of_birth: int | None = None
    gender: Literal["male", "female", "other", "undisclosed"] | None = None
    # ABHA path: the reference ABDM returned. Never an Aadhaar number.
    abha_reference: str | None = None
    # Local path
    hospital_local_id: str | None = None
    phone_last4: str | None = None


class IdentifyResponse(BaseModel):
    patient_id: UUID
    display_name: str
    hospital_local_id: str | None
    has_abha: bool
    preferred_language: str
    is_new_registration: bool
    consent_required: bool


@router.post("/identify", response_model=IdentifyResponse)
async def identify(
    ctx: Ctx,
    principal: KioskPrincipal,
    payload: IdentifyRequest,
) -> IdentifyResponse:
    """Identify or register the patient standing at the kiosk.

    The local path is always available and is never blocked (§7.1) — a patient
    without an ABHA card must still be seen today.
    """
    language = ctx.localization.normalize(payload.preferred_language)

    async with ctx.db.transaction(principal) as conn:
        if payload.mode == "abha":
            if not payload.abha_reference:
                raise ValidationFailed(
                    "abha_reference is required for the ABHA path",
                    reason_code="abha_reference_required",
                )
            record = await identity.upsert_from_abha(
                conn,
                principal,
                abha_reference=payload.abha_reference,
                full_name=payload.full_name,
                year_of_birth=payload.year_of_birth,
                gender=payload.gender,
                preferred_language=language,
            )
        else:
            record = await identity.register_local(
                conn,
                principal,
                hospital_local_id=payload.hospital_local_id,
                full_name=payload.full_name,
                year_of_birth=payload.year_of_birth,
                gender=payload.gender,
                phone_last4=payload.phone_last4,
                preferred_language=language,
            )

    return IdentifyResponse(
        patient_id=record.id,
        display_name=record.full_name,
        hospital_local_id=record.hospital_local_id,
        has_abha=record.abha_reference is not None,
        preferred_language=record.preferred_language,
        is_new_registration=record.is_new,
        consent_required=True,
    )


@router.get("/abha/create-handoff")
async def abha_create_handoff(ctx: Ctx, _principal: KioskPrincipal) -> dict[str, Any]:
    """Hand the patient off to ABDM's own ABHA-creation flow (§7.1).

    [RED LINE §7.1] MediKiosk does not run e-KYC and never receives an Aadhaar
    number. We hand over, and we receive an ABHA reference back.

    [MOCK/SANDBOX §23] This points at ABDM's sandbox. It is labelled as such in
    the response so the UI cannot present it as production access.
    """
    return {
        "environment": ctx.settings.abdm_environment,
        "handoff_url": f"{ctx.settings.abdm_base_url.rstrip('/')}/abha/registration",
        "notice_key": "identity.create_abha_note",
        "medikiosk_receives": ["abha_reference"],
        "medikiosk_never_receives": ["aadhaar_number", "aadhaar_otp", "biometrics"],
        "is_sandbox": ctx.settings.abdm_environment == "sandbox",
    }


class LanguageRequest(BaseModel):
    language: str


@router.post("/patients/{patient_id}/language")
async def set_patient_language(
    ctx: Ctx,
    principal: KioskPrincipal,
    patient_id: UUID,
    payload: LanguageRequest,
) -> dict[str, str]:
    language = ctx.localization.normalize(payload.language)
    async with ctx.db.transaction(principal) as conn:
        await identity.set_language(conn, patient_id, language)
    return {"language": language}
