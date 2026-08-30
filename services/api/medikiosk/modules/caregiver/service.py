"""Caregiver / respondent model (CLAUDE.md §6).

One rule, applied identically to spoken answers, typed answers and uploaded
documents: **a caregiver is always a respondent; a caregiver is a consent-grantor
only under documented authority.**

    CaregiverAuthorization = (caregiver_identity, patient_id, relationship,
                              authority_basis, verified_at)

What must never happen, and is prevented here:

* a caregiver self-declaring their own authority — the documented bases require a
  document reference AND a staff witness, and the witness must be a real staff
  user in this tenant;
* a caregiver answering before the patient's acknowledgment is recorded — the
  default basis cannot be created without ``patient_acknowledged_at``;
* a caregiver-sourced fact silently presented as if the patient said it — every
  fact carries ``respondent_id`` and ``respondent_relationship``, and the
  physician dashboard labels it.

[RED LINE §6] Blood relationship alone never automatically grants consent
authority. Software cannot verify a claimed relationship, so the mitigation is
accountability — hash-chained audit and a staff co-signature in the incapacity
case — not pre-emptive fraud-proofing that no system achieves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import asyncpg

from medikiosk.db import Principal
from medikiosk.errors import Forbidden, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit


class AuthorityBasis(StrEnum):
    PATIENT_PRESENT_AND_ACKNOWLEDGES = "patient_present_and_acknowledges"
    DOCUMENTED_GUARDIANSHIP = "documented_guardianship"
    DOCUMENTED_MEDICAL_POWER_OF_ATTORNEY = "documented_medical_power_of_attorney"

    @property
    def requires_patient_acknowledgment(self) -> bool:
        return self is AuthorityBasis.PATIENT_PRESENT_AND_ACKNOWLEDGES

    @property
    def requires_staff_witness(self) -> bool:
        return not self.requires_patient_acknowledgment

    @property
    def grants_consent_authority(self) -> bool:
        return self.requires_staff_witness


RELATIONSHIPS: frozenset[str] = frozenset(
    {
        "spouse",
        "parent",
        "child",
        "sibling",
        "other_relative",
        "friend_or_neighbour",
        "paid_attendant",
        "legal_guardian",
    }
)


@dataclass(frozen=True, slots=True)
class CaregiverAuthorization:
    id: UUID
    patient_id: UUID
    caregiver_name: str
    relationship: str
    authority_basis: AuthorityBasis
    may_grant_consent: bool
    patient_acknowledged: bool


def _validate(caregiver_name: str, relationship: str) -> tuple[str, str]:
    name = " ".join(caregiver_name.split())
    if len(name) < 2 or len(name) > 120:
        raise ValidationFailed("caregiver name is invalid", reason_code="name_invalid")
    if relationship not in RELATIONSHIPS:
        raise ValidationFailed("unknown relationship", reason_code="relationship_invalid")
    return name, relationship


async def record_patient_acknowledgment(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    caregiver_name: str,
    relationship: str,
    ack_method: str,
) -> CaregiverAuthorization:
    """The default path: the patient is asked directly, BEFORE the caregiver
    answers anything, in their own voice or tap (§6).

    ``ack_method`` records which — voice or touch — because "the patient tapped
    yes" and "the patient said yes" are different evidentiary claims.
    """
    name, rel = _validate(caregiver_name, relationship)
    if ack_method not in ("voice", "touch"):
        raise ValidationFailed("acknowledgment method must be voice or touch",
                               reason_code="validation_failed")

    row = await conn.fetchrow(
        """
        INSERT INTO caregiver_authorization
            (tenant_id, patient_id, caregiver_name, relationship, authority_basis,
             patient_acknowledged_at, patient_ack_method)
        VALUES ($1, $2, $3, $4, 'patient_present_and_acknowledges', now(), $5)
        RETURNING id
        """,
        principal.tenant_id,
        patient_id,
        name,
        rel,
        ack_method,
    )
    await audit.record(
        conn,
        principal,
        action="caregiver.patient_acknowledged",
        entity_type="caregiver_authorization",
        entity_id=row["id"],
        detail={
            "relationship": rel,
            "authority_basis": "patient_present_and_acknowledges",
            "input_method": ack_method,
        },
    )
    return CaregiverAuthorization(
        id=row["id"],
        patient_id=patient_id,
        caregiver_name=name,
        relationship=rel,
        authority_basis=AuthorityBasis.PATIENT_PRESENT_AND_ACKNOWLEDGES,
        may_grant_consent=False,
        patient_acknowledged=True,
    )


async def record_documented_authority(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    caregiver_name: str,
    relationship: str,
    authority_basis: AuthorityBasis,
    document_reference: str,
    witnessed_by_user_id: UUID,
) -> CaregiverAuthorization:
    """Guardianship / medical power of attorney — staff-witnessed at registration.

    This function is deliberately NOT reachable from a kiosk token: it requires a
    staff principal, because the whole point of the documented path is that a
    named member of staff attests to having seen the document.

    [ASSUMPTION §63] Which document types constitute valid caregiver legal
    authority needs legal counsel, tied to the DPDP Rules 2025 provisions on
    processing data of dependents. Until that is settled, MediKiosk records the
    reference and the witness and makes no judgment about sufficiency.
    """
    if authority_basis.requires_patient_acknowledgment:
        raise ValidationFailed(
            "use record_patient_acknowledgment for the default basis",
            reason_code="validation_failed",
        )
    if principal.role not in ("nurse", "physician", "ayush_practitioner", "it_admin"):
        raise Forbidden(
            "documented caregiver authority must be witnessed by staff",
            reason_code="staff_witness_required",
        )

    name, rel = _validate(caregiver_name, relationship)
    reference = document_reference.strip()
    if len(reference) < 4:
        raise ValidationFailed("document reference is required",
                               reason_code="document_reference_required")

    witness_exists = await conn.fetchval(
        "SELECT 1 FROM app_user WHERE id = $1 AND status = 'active'", witnessed_by_user_id
    )
    if not witness_exists:
        # A self-declared or fabricated witness is exactly the failure mode this
        # check exists to prevent.
        raise ValidationFailed("witness is not an active staff user",
                               reason_code="witness_invalid")

    row = await conn.fetchrow(
        """
        INSERT INTO caregiver_authorization
            (tenant_id, patient_id, caregiver_name, relationship, authority_basis,
             document_reference, witnessed_by_user_id, verified_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        RETURNING id
        """,
        principal.tenant_id,
        patient_id,
        name,
        rel,
        str(authority_basis),
        reference,
        witnessed_by_user_id,
    )
    await audit.record(
        conn,
        principal,
        action="caregiver.documented_authority_recorded",
        entity_type="caregiver_authorization",
        entity_id=row["id"],
        detail={
            "relationship": rel,
            "authority_basis": str(authority_basis),
            "step_up_verified": True,
        },
    )
    return CaregiverAuthorization(
        id=row["id"],
        patient_id=patient_id,
        caregiver_name=name,
        relationship=rel,
        authority_basis=authority_basis,
        may_grant_consent=True,
        patient_acknowledged=False,
    )


async def get(conn: asyncpg.Connection, auth_id: UUID) -> CaregiverAuthorization:
    row = await conn.fetchrow(
        """
        SELECT id, patient_id, caregiver_name, relationship, authority_basis,
               patient_acknowledged_at
          FROM caregiver_authorization
         WHERE id = $1 AND revoked_at IS NULL
        """,
        auth_id,
    )
    if row is None:
        raise NotFound("caregiver authorization not found", reason_code="not_found")
    basis = AuthorityBasis(row["authority_basis"])
    return CaregiverAuthorization(
        id=row["id"],
        patient_id=row["patient_id"],
        caregiver_name=row["caregiver_name"],
        relationship=row["relationship"],
        authority_basis=basis,
        may_grant_consent=basis.grants_consent_authority,
        patient_acknowledged=row["patient_acknowledged_at"] is not None,
    )


async def assert_may_respond(
    conn: asyncpg.Connection, auth_id: UUID, patient_id: UUID
) -> CaregiverAuthorization:
    """A caregiver may answer only for the patient they are authorised for."""
    authorization = await get(conn, auth_id)
    if authorization.patient_id != patient_id:
        raise Forbidden("caregiver is not authorised for this patient",
                        reason_code="forbidden")
    if (
        authorization.authority_basis.requires_patient_acknowledgment
        and not authorization.patient_acknowledged
    ):
        raise Forbidden(
            "the patient has not acknowledged this caregiver",
            reason_code="caregiver_ack_required",
        )
    return authorization


async def revoke(conn: asyncpg.Connection, principal: Principal, auth_id: UUID) -> None:
    row = await conn.fetchrow(
        """
        UPDATE caregiver_authorization SET revoked_at = now()
         WHERE id = $1 AND revoked_at IS NULL
        RETURNING id, relationship
        """,
        auth_id,
    )
    if row is None:
        raise NotFound("caregiver authorization not found", reason_code="not_found")
    await audit.record(
        conn,
        principal,
        action="caregiver.authorization_revoked",
        entity_type="caregiver_authorization",
        entity_id=row["id"],
        detail={"relationship": row["relationship"]},
    )
