"""Internal consent and caregiver acknowledgment endpoints (§6, §7.2)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, KioskPrincipal
from medikiosk.errors import ValidationFailed
from medikiosk.modules.caregiver import service as caregiver_service
from medikiosk.modules.caregiver.service import RELATIONSHIPS
from medikiosk.modules.consent import service as consent_service
from medikiosk.modules.consent.service import ConsentGrant, Purpose

router = APIRouter(prefix="/v1", tags=["consent"])


class ConsentDecision(BaseModel):
    purpose: Literal[
        "voice_capture",
        "document_processing",
        "ai_processing",
        "staff_access",
        "abdm_sharing_intent",
    ]
    granted: bool


class ConsentRequest(BaseModel):
    patient_id: UUID
    decisions: list[ConsentDecision] = PField(min_length=1)
    notice_language: str
    # Proof that the notice was actually played, not merely displayed (§7.2).
    audio_explained: bool = False
    grantor_type: Literal["patient", "caregiver"] = "patient"
    caregiver_auth_id: UUID | None = None


class ConsentResponse(BaseModel):
    patient_id: UUID
    granted: list[str]
    refused: list[str]
    notice_version: str
    may_proceed: bool


@router.post("/consents", response_model=ConsentResponse)
async def grant_consents(
    ctx: Ctx, principal: KioskPrincipal, payload: ConsentRequest
) -> ConsentResponse:
    """Record the patient's (or authorised caregiver's) consent decisions.

    The notice version comes from the *content bundle*, not the request, so what
    is stored is provably the wording the person actually heard (§7.2).
    """
    language = ctx.localization.normalize(payload.notice_language)
    notice_version = ctx.localization.bundle(language, "consent").get("notice_version")
    if not notice_version:
        raise ValidationFailed(
            "consent notice version is missing from the content bundle",
            reason_code="consent_notice_missing",
        )

    grants = [
        ConsentGrant(purpose=Purpose(d.purpose), granted=d.granted) for d in payload.decisions
    ]

    async with ctx.db.transaction(principal) as conn:
        state = await consent_service.record_consents(
            conn,
            principal,
            patient_id=payload.patient_id,
            grants=grants,
            notice_version=notice_version,
            notice_language=language,
            audio_explained=payload.audio_explained,
            grantor_type=payload.grantor_type,
            caregiver_auth_id=payload.caregiver_auth_id,
        )

    all_purposes = {p for p in Purpose}
    return ConsentResponse(
        patient_id=payload.patient_id,
        granted=sorted(str(p) for p in state.granted),
        refused=sorted(str(p) for p in (all_purposes - state.granted)),
        notice_version=notice_version,
        may_proceed=state.allows(Purpose.STAFF_ACCESS),
    )


@router.get("/consents/{patient_id}", response_model=ConsentResponse)
async def get_consents(
    ctx: Ctx, principal: KioskPrincipal, patient_id: UUID
) -> ConsentResponse:
    async with ctx.db.readonly(principal) as conn:
        state = await consent_service.current_state(conn, patient_id)
    all_purposes = {p for p in Purpose}
    return ConsentResponse(
        patient_id=patient_id,
        granted=sorted(str(p) for p in state.granted),
        refused=sorted(str(p) for p in (all_purposes - state.granted)),
        notice_version=state.notice_version or "",
        may_proceed=state.allows(Purpose.STAFF_ACCESS),
    )


@router.delete("/consents/{consent_id}")
async def revoke_consent(
    ctx: Ctx, principal: KioskPrincipal, consent_id: UUID
) -> dict[str, Any]:
    """``DELETE /v1/consents/{id}`` (§7.2) — revocation, always available.

    The row is marked revoked rather than deleted: the record of what was agreed
    is itself evidence, and destroying it would defeat the audit.
    """
    async with ctx.db.transaction(principal) as conn:
        state = await consent_service.revoke(conn, principal, consent_id=consent_id)
    return {
        "revoked": True,
        "granted": sorted(str(p) for p in state.granted),
        "may_proceed": state.allows(Purpose.STAFF_ACCESS),
    }


class RevokePurposeRequest(BaseModel):
    patient_id: UUID
    purpose: Literal[
        "voice_capture",
        "document_processing",
        "ai_processing",
        "staff_access",
        "abdm_sharing_intent",
    ]


@router.post("/consents/revoke-purpose")
async def revoke_purpose(
    ctx: Ctx, principal: KioskPrincipal, payload: RevokePurposeRequest
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        state = await consent_service.revoke_purpose(
            conn,
            principal,
            patient_id=payload.patient_id,
            purpose=Purpose(payload.purpose),
        )
    return {
        "revoked": payload.purpose,
        "granted": sorted(str(p) for p in state.granted),
        "may_proceed": state.allows(Purpose.STAFF_ACCESS),
    }


# ---------------------------------------------------------------------------
# Caregiver acknowledgment (§6)
# ---------------------------------------------------------------------------
class CaregiverAcknowledgeRequest(BaseModel):
    patient_id: UUID
    caregiver_name: str = PField(min_length=2, max_length=120)
    relationship: str
    # Recorded in the patient's OWN voice or tap, before the caregiver answers.
    patient_ack_method: Literal["voice", "touch"]


class CaregiverAcknowledgeResponse(BaseModel):
    caregiver_auth_id: UUID
    relationship: str
    authority_basis: str
    may_grant_consent: bool
    facts_will_be_labelled: str


@router.post("/caregivers/acknowledge", response_model=CaregiverAcknowledgeResponse)
async def caregiver_acknowledge(
    ctx: Ctx, principal: KioskPrincipal, payload: CaregiverAcknowledgeRequest
) -> CaregiverAcknowledgeResponse:
    """The patient acknowledges a caregiver as respondent (§6).

    This is the ONLY caregiver path reachable from a kiosk. It confers the right
    to *answer*, never the right to consent — ``may_grant_consent`` is returned
    as ``false`` so the UI cannot mistakenly offer the consent screen to the
    caregiver.

    Documented guardianship / medical power of attorney are staff-witnessed and
    live on the staff surface, because the whole point is that a named member of
    staff attests to having seen the document.
    """
    if payload.relationship not in RELATIONSHIPS:
        raise ValidationFailed(
            "unknown relationship", reason_code="relationship_invalid",
            detail={"allowed": sorted(RELATIONSHIPS)},
        )

    async with ctx.db.transaction(principal) as conn:
        authorization = await caregiver_service.record_patient_acknowledgment(
            conn,
            principal,
            patient_id=payload.patient_id,
            caregiver_name=payload.caregiver_name,
            relationship=payload.relationship,
            ack_method=payload.patient_ack_method,
        )

    return CaregiverAcknowledgeResponse(
        caregiver_auth_id=authorization.id,
        relationship=authorization.relationship,
        authority_basis=str(authorization.authority_basis),
        may_grant_consent=False,
        facts_will_be_labelled=(
            f"Reported by: {authorization.caregiver_name}, "
            f"relationship: {authorization.relationship}"
        ),
    )


@router.get("/caregivers/relationships")
async def caregiver_relationships() -> dict[str, list[str]]:
    return {"relationships": sorted(RELATIONSHIPS)}
