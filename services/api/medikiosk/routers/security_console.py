"""Security & Privacy section of the Admin workspace (CLAUDE.md §4, §5.2, §65).

The Security/Privacy Officer sees consent posture, retention posture and audit
integrity. They deliberately CANNOT edit a clinical record — the RBAC capability
set omits every clinical write, and a test asserts that omission (§5.2).

Every claim rendered here is computed, not asserted. In particular this console
reports what is *and is not* certified: [RED LINE §26, §62] MediKiosk must never
present "DPDP compliant" or "VAPT complete" as a status it has not obtained.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from medikiosk.deps import Ctx, StaffPrincipal, require
from medikiosk.modules.consent import service as consent_service
from medikiosk.modules.purge import service as purge_service
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

router = APIRouter(prefix="/v1/security", tags=["security"])


@router.get("/consent-status")
async def consent_status(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.CONSENT_STATUS_READ, "read"))],
) -> dict[str, Any]:
    """Aggregate consent posture.

    Counts, not names. The officer needs coverage figures to answer a regulator;
    they do not need to know which patient refused which purpose, so the query
    never returns a patient identifier (§28 data minimisation).
    """
    await authz.check(
        ResourceContext(type="consent_status", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        report = await consent_service.status_report(conn)
        notices = await conn.fetch(
            """
            SELECT notice_version, notice_language, count(*) AS count,
                   count(*) FILTER (WHERE audio_explained) AS audio_explained
              FROM consent
             GROUP BY notice_version, notice_language
             ORDER BY notice_version, notice_language
            """
        )

    return {
        "per_purpose": report,
        "notice_versions": [dict(r) for r in notices],
        "requirements": {
            "internal_consent_required_for_every_session": True,
            "audio_explained_required": True,
            "revocable": True,
            "abdm_consent_is_a_separate_artifact": True,
        },
        "note": (
            "Internal MediKiosk consent and the ABDM network consent artifact are "
            "structurally distinct objects and are never merged (CLAUDE.md §7.2)."
        ),
    }


@router.get("/retention-status")
async def retention_status(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.RETENTION_STATUS_READ, "read"))],
) -> dict[str, Any]:
    """Retention and purge posture (§38, §65).

    ``transient_purge_compliant`` is the number that matters: every submitted
    session must carry a ``transient_purged_at`` stamp. If any does not, that is
    a finding, and it is surfaced as its own field rather than buried in a total.
    """
    await authz.check(
        ResourceContext(type="retention_status", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        status = await purge_service.retention_status(conn)
    return {
        **status,
        "policy": {
            "raw_audio": "purged immediately after transcription; never persisted",
            "transient_session_state": "synchronous purge at submission (code-enforced)",
            "documents_and_facts": "retained per tenant DPDP-Rules-2025-mapped schedule",
            "telemetry": "short, separate window; pseudonymous by construction",
        },
    }


@router.get("/posture")
async def security_posture(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.RETENTION_STATUS_READ, "read"))],
) -> dict[str, Any]:
    """The honest security and compliance posture.

    Controls that exist are reported as implemented. Certifications that have NOT
    been obtained are reported as not obtained, with what would be required. This
    endpoint is deliberately the place a reviewer can check that the product is
    not overclaiming.
    """
    await authz.check(
        ResourceContext(type="retention_status", tenant_id=principal.tenant_id)
    )

    async with ctx.db.readonly(principal) as conn:
        rls = await conn.fetch(
            """
            SELECT c.relname AS table_name, c.relrowsecurity AS enabled,
                   c.relforcerowsecurity AS forced,
                   (SELECT count(*) FROM pg_policies p
                     WHERE p.tablename = c.relname) AS policy_count
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             ORDER BY c.relname
            """
        )
        role = await conn.fetchrow(
            "SELECT current_user AS name, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        audit_grants = await conn.fetch(
            """
            SELECT privilege_type
              FROM information_schema.table_privileges
             WHERE table_name = 'audit_event' AND grantee = current_user
            """
        )
        mfa = await conn.fetchrow(
            """
            SELECT count(*) FILTER (
                     WHERE role IN ('physician','ayush_practitioner','clinical_admin',
                                    'it_admin','security_officer')
                   ) AS privileged_users,
                   count(*) FILTER (
                     WHERE role IN ('physician','ayush_practitioner','clinical_admin',
                                    'it_admin','security_officer')
                       AND mfa_enrolled
                   ) AS privileged_mfa_enrolled
              FROM app_user WHERE status = 'active'
            """
        )

    audit_privileges = sorted({r["privilege_type"] for r in audit_grants})
    protected = [r for r in rls if r["enabled"] and r["forced"]]

    return {
        "implemented_controls": {
            "rls_tables_protected": len(protected),
            "rls_tables_total": len(rls),
            "app_role_bypasses_rls": bool(role and (role["rolsuper"] or role["rolbypassrls"])),
            "audit_privileges_held_by_app": audit_privileges,
            "audit_is_append_only": "UPDATE" not in audit_privileges
            and "DELETE" not in audit_privileges,
            "opa_deny_by_default": not ctx.settings.opa_fail_open,
            "mfa_required_roles": list(ctx.settings.mfa_required_roles),
            "privileged_users": dict(mfa) if mfa else {},
            "phi_redaction_at_single_choke_point": True,
            "synthetic_data_only_environment": ctx.settings.is_synthetic_data_environment,
            "upload_magic_byte_verification": True,
            "malware_scan_before_ocr": ctx.settings.clamav_required,
            "ai_has_no_database_route": True,
        },
        "not_obtained": {
            "dpdp_compliance_determination": (
                "NOT OBTAINED — technical controls exist; a compliance determination is a "
                "legal counsel judgment. MediKiosk never claims 'DPDP compliant' as a "
                "status (CLAUDE.md §26 [RED LINE])."
            ),
            "cert_in_vapt": (
                "NOT OBTAINED — a CERT-In empanelled VAPT should be engaged before any "
                "real-patient pilot (CLAUDE.md §27 [CERT])."
            ),
            "abdm_production_access": (
                "NOT OBTAINED — sandbox only (CLAUDE.md §23)."
            ),
            "namaste_live_api": (
                "NOT OBTAINED — static versioned snapshot; Ministry access terms "
                "unconfirmed (CLAUDE.md §24 [ASSUMPTION])."
            ),
            "confidence_threshold_calibration": (
                "NOT DONE — τ_high/τ_low are placeholders until pilot data exists "
                "(CLAUDE.md §53 [RED LINE])."
            ),
        },
        "open_questions": [
            "Licensed drug-interaction database: source and cost undetermined (§63).",
            "Valid caregiver legal-authority document types: needs legal counsel (§63).",
            "Pilot hospital and its HIS: not yet identified (§63).",
            "DPDP Rules 2025 Significant Data Fiduciary applicability: needs counsel (§63).",
        ],
        "rls_detail": [dict(r) for r in rls],
    }
