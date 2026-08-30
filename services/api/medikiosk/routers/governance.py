"""Clinical Governance section of the Admin workspace (CLAUDE.md §4, §5.2).

Governance is a permission-gated SECTION, not a separate application (§4). The
Clinical Admin role reads clinical CONTENT and clinical SAFETY METRICS. It has
deliberately no access to clinical records, and no write path at all: content
changes route through the CI governance gate (§46, §61), not through this API.

That is why every endpoint here is a GET.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from medikiosk.deps import Ctx, StaffPrincipal, require
from medikiosk.modules.triage import service as triage_service
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

router = APIRouter(prefix="/v1/governance", tags=["governance"])


@router.get("/protocols")
async def protocols(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.GOVERNANCE_PROTOCOL_READ, "read"))],
) -> dict[str, Any]:
    """The protocol content actually loaded, with checksums.

    The checksum is the point: it lets a reviewer prove the running system is
    executing byte-identical content to what the Board approved (§10, §46).
    """
    await authz.check(
        ResourceContext(type="protocol_version", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        approved = await conn.fetch(
            """
            SELECT protocol_family, version, display_name, content_checksum, status,
                   governance_reviewer, approved_at
              FROM protocol_version
             ORDER BY protocol_family, version
            """
        )
        active = await conn.fetch(
            "SELECT protocol_family, active_version, updated_at FROM tenant_protocol_config"
        )

    loaded = ctx.protocols.describe()
    approved_map = {(r["protocol_family"], r["version"]): dict(r) for r in approved}

    return {
        "active_versions": [dict(r) for r in active],
        "loaded": [
            {
                "family": d.family,
                "version": d.version,
                "checksum": d.checksum,
                "field_count": d.field_count,
                "required_count": d.required_count,
                "ample_count": d.ample_count,
                "groups": list(d.groups),
                "governance": approved_map.get((d.family, d.version)),
                "checksum_matches_approval": (
                    approved_map.get((d.family, d.version), {}).get("content_checksum")
                    in (None, d.checksum)
                ),
            }
            for d in loaded
        ],
        "review_requirement": (
            "Protocol content changes require the clinical-safety-reviewer agent and "
            "the CI clinical-governance gate before merge (CLAUDE.md §46, §59, §61)."
        ),
    }


@router.get("/protocols/{family}/{version}/fields")
async def protocol_fields(
    ctx: Ctx,
    family: str,
    version: str,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.GOVERNANCE_PROTOCOL_READ, "read"))],
    language: str = "en",
) -> dict[str, Any]:
    """Every field, rendered in one language, for clinical review.

    Showing the localized wording beside the language-neutral definition is what
    lets a reviewer check that the clinical intent and the patient-facing words
    actually agree.
    """
    await authz.check(
        ResourceContext(type="protocol_version", tenant_id=principal.tenant_id)
    )
    protocol = ctx.protocols.load(family, version)
    code = ctx.localization.normalize(language)

    return {
        "family": family,
        "version": version,
        "checksum": protocol.content_checksum,
        "language": code,
        "fields": [
            {
                "field_id": field.id,
                "concept_code": field.concept_code,
                "category": field.category,
                "group": field.group,
                "order": field.order,
                "required": field.required,
                "ample": field.ample,
                "red_flag_input": field.red_flag_input,
                "confirm_back": field.confirm_back,
                "value_type": str(field.value_type),
                "widget": str(field.widget),
                "depends_on": field.depends_on,
                "tau_high": field.tau_high,
                "tau_low": field.tau_low,
                "rendered": {
                    "voice_prompt": rendered.voice_prompt,
                    "touch_label": rendered.touch_label,
                    "help": rendered.help,
                    "options": [
                        {"value": o.value, "label": o.label, "icon": o.icon}
                        for o in rendered.options
                    ],
                },
            }
            for field in (protocol.fields[fid] for fid in protocol.ordering)
            if (rendered := ctx.localization.render_field(protocol, field, code))
        ],
    }


@router.get("/red-flag-rules")
async def red_flag_rules(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.GOVERNANCE_REDFLAG_READ, "read"))],
) -> dict[str, Any]:
    """The active safety ruleset, with its clinical rationale per rule."""
    await authz.check(
        ResourceContext(type="red_flag_ruleset", tenant_id=principal.tenant_id)
    )
    ruleset = ctx.ruleset
    protocols = tuple(
        ctx.protocols.load(d.family, d.version) for d in ctx.protocols.describe()
    )
    disarmed = set(ctx.red_flags.validate_against(ruleset, protocols))

    return {
        "version": ruleset.version,
        "checksum": ruleset.content_checksum,
        "calibration_status": (
            "UNCALIBRATED — sensitivity and thresholds are placeholders until pilot "
            "data exists (CLAUDE.md §53)"
        ),
        "counts": {
            "total": len(ruleset.rules),
            "active": len(ruleset.active_rules()),
            "critical": sum(1 for r in ruleset.rules if r.severity == "critical"),
            "high": sum(1 for r in ruleset.rules if r.severity == "high"),
            "moderate": sum(1 for r in ruleset.rules if r.severity == "moderate"),
            "disarmed": len(disarmed),
        },
        "rules": [
            {
                "rule_id": rule.id,
                "name": rule.name,
                "severity": str(rule.severity),
                "sla_seconds": rule.sla_seconds,
                "category": rule.category,
                "active": rule.active,
                "input_fields": list(rule.input_fields),
                "predicate": rule.predicate,
                "staff_rationale": rule.staff_rationale,
                "clinical_reference": rule.reference,
                "creates_alert": rule.severity.creates_alert,
                "triggers_fast_path": rule.severity.triggers_fast_path,
                "disarmed": rule.id in disarmed,
            }
            for rule in ruleset.rules
        ],
    }


@router.get("/safety-metrics")
async def safety_metrics(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.GOVERNANCE_METRICS_READ, "read"))],
) -> dict[str, Any]:
    """Fire rates per rule, plus escalation outcomes.

    This is the measurement §14 exists to make possible: every evaluation is
    persisted, fired or not, so a rule that never fires and a rule that fires on
    everything are both visible instead of assumed away.
    """
    await authz.check(
        ResourceContext(type="clinical_metrics", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        per_rule = await triage_service.evaluation_stats(conn)
        outcomes = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM red_flag_alert)                            AS alerts_total,
              (SELECT count(*) FROM red_flag_alert WHERE status = 'open')       AS alerts_open,
              (SELECT count(*) FROM red_flag_alert WHERE acknowledged_at IS NOT NULL)
                                                                               AS alerts_acked,
              (SELECT count(*) FROM red_flag_alert
                WHERE acknowledged_at IS NULL AND status = 'open'
                  AND created_at < now() - make_interval(secs => sla_seconds))
                                                                               AS sla_breached,
              (SELECT count(*) FROM session WHERE fast_path_active)            AS fast_path_sessions,
              (SELECT count(*) FROM session)                                   AS sessions_total,
              (SELECT count(*) FROM session WHERE status = 'completed')         AS sessions_completed,
              (SELECT count(*) FROM session WHERE status = 'escalated_to_staff')
                                                                               AS sessions_escalated,
              (SELECT count(*) FROM fact_conflict WHERE resolution = 'unresolved')
                                                                               AS conflicts_open,
              (SELECT count(*) FROM extraction_candidate WHERE status = 'pending')
                                                                               AS verification_pending,
              (SELECT round(avg(completeness) * 100, 1) FROM session
                WHERE submitted_at IS NOT NULL)                                AS avg_completeness_pct,
              (SELECT count(*) FROM summary WHERE generation_mode = 'structured_fallback')
                                                                               AS summaries_fallback,
              (SELECT count(*) FROM summary WHERE generation_mode = 'llm_drafted')
                                                                               AS summaries_llm
            """
        )
        by_severity = await conn.fetch(
            """
            SELECT severity, count(*) AS fired
              FROM red_flag_alert GROUP BY severity ORDER BY severity
            """
        )

    return {
        "ruleset_version": ctx.ruleset.version,
        "per_rule": per_rule,
        "by_severity": [dict(r) for r in by_severity],
        "outcomes": dict(outcomes) if outcomes else {},
        "interpretation_note": (
            "Fire rates are descriptive only. False-positive and false-negative rates "
            "cannot be computed without adjudicated ground truth from a pilot (§53)."
        ),
    }


@router.get("/localization-coverage")
async def localization_coverage(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.GOVERNANCE_PROTOCOL_READ, "read"))],
) -> dict[str, Any]:
    """Translation coverage per protocol and language.

    Startup already refuses to run with a gap, so in a healthy system every
    number here is zero — which is exactly what a governance reviewer needs to
    be able to confirm rather than assume.
    """
    await authz.check(
        ResourceContext(type="protocol_version", tenant_id=principal.tenant_id)
    )
    protocols = tuple(
        ctx.protocols.load(d.family, d.version) for d in ctx.protocols.describe()
    )
    gaps = {p.key: ctx.localization.missing_translations(p) for p in protocols}

    return {
        "supported_languages": [
            {"code": p.code, "endonym": p.endonym, "english_name": p.english_name}
            for p in ctx.localization.languages
        ],
        "protocol_gaps": {
            key: {lang: len(missing) for lang, missing in per_lang.items()}
            for key, per_lang in gaps.items()
        },
        "ui_gaps": {lang: len(keys) for lang, keys in ctx.localization.missing_ui_keys().items()},
        "complete": not any(gaps.values()) and not ctx.localization.missing_ui_keys(),
    }
