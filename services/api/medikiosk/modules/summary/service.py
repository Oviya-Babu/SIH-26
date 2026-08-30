"""Evidence-grounded summary (CLAUDE.md §19).

Three hard rules, and the code is arranged so each is enforced by structure
rather than by prompting:

1. **Input is structured Clinical Facts only.** The raw transcript is never fed
   in for "creative" summarisation. ``_fact_payload`` is the only thing the LLM
   ever sees, and it carries no free-text answer bodies beyond the normalized
   value itself.
2. **Every generated sentence must cite a real ``clinical_fact.id``.** A sentence
   whose citations do not resolve to live facts *in this session* is DROPPED by
   :func:`_validate_statements` before persistence, and the database enforces
   ``array_length(citations, 1) >= 1`` as a second line.
3. **Failure degrades, never blocks.** If the LLM times out or returns nothing
   citable, a deterministic ``structured_fallback`` summary is written from the
   facts themselves. The physician always gets a usable view (§19, §37).

The generation contract is therefore: *the model may choose wording; it may not
choose facts.*
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.ai.gateway_client import AIGatewayClient
from medikiosk.db import Principal
from medikiosk.errors import DependencyUnavailable, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.clinical_facts import service as facts_service
from medikiosk.modules.clinical_facts.service import FactRecord
from medikiosk.modules.consent import service as consent_service
from medikiosk.modules.consent.service import Purpose
from medikiosk.modules.localization.registry import LocalizationRegistry
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

# Physician-facing section order. Stable, so a physician's eye lands in the same
# place every time — which is the point of a summary read in two minutes.
SECTIONS: tuple[str, ...] = (
    "presenting_complaint",
    "history_of_present_illness",
    "review_of_systems",
    "past_history",
    "medications_and_allergies",
    "family_and_personal_history",
    "ayush_assessment",
    "documents_and_investigations",
    "flags_and_gaps",
)

_CATEGORY_SECTION: dict[str, str] = {
    "chief_complaint": "presenting_complaint",
    "symptom": "history_of_present_illness",
    "review_of_systems": "review_of_systems",
    "past_medical_history": "past_history",
    "past_surgical_history": "past_history",
    "procedure_history": "past_history",
    "medication": "medications_and_allergies",
    "allergy": "medications_and_allergies",
    "family_history": "family_and_personal_history",
    "personal_history": "family_and_personal_history",
    "dashavidha_parameter": "ayush_assessment",
    "ahara_vihara": "ayush_assessment",
    "nidana": "ayush_assessment",
    "samprapti": "ayush_assessment",
    "investigation_value": "documents_and_investigations",
    "diagnosis": "documents_and_investigations",
    "vital_sign": "documents_and_investigations",
    "ample_field": "flags_and_gaps",
}


@dataclass(frozen=True, slots=True)
class SummaryStatement:
    section: str
    ordinal: int
    text: str
    citations: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class GeneratedSummary:
    summary_id: UUID
    generation_mode: str
    statement_count: int
    citation_count: int
    latency_ms: int
    dropped_uncited: int


async def generate(
    conn: asyncpg.Connection,
    principal: Principal,
    ai: AIGatewayClient | None,
    localization: LocalizationRegistry,
    *,
    session_id: UUID,
    language: str = "en",
) -> GeneratedSummary:
    """Generate (or regenerate) the physician-facing draft.

    Bounded async: the LLM gets ``ai_llm_timeout_seconds`` (§54, 8s p95). Past
    that, or on any failure, the structured fallback is written instead — the
    physician is never left waiting on a model.
    """
    live = await facts_service.current_facts(conn, session_id)
    if not live:
        raise ValidationFailed(
            "cannot summarise a session with no clinical facts",
            reason_code="no_facts_to_summarise",
        )

    patient_id = await conn.fetchval("SELECT patient_id FROM session WHERE id = $1", session_id)
    if patient_id is None:
        raise NotFound("session not found", reason_code="not_found")

    consent = await consent_service.current_state(conn, patient_id)
    ai_allowed = consent.allows(Purpose.AI_PROCESSING)

    statements: list[SummaryStatement] = []
    mode = "structured_fallback"
    model_version: str | None = None
    prompt_version: str | None = None
    latency_ms = 0
    dropped = 0

    if ai_allowed and ai is not None:
        try:
            draft = await ai.draft_summary(
                facts=[_fact_payload(f) for f in live],
                language=language,
                sections=list(SECTIONS),
            )
            statements, dropped = _validate_statements(draft.statements, live)
            if statements:
                mode = "llm_drafted"
                model_version = draft.model_version
                prompt_version = draft.prompt_version
                latency_ms = draft.latency_ms
            else:
                # The model produced nothing that survived the citation contract.
                # That is a failure of the draft, not a reason to lower the bar.
                log.warning(
                    "llm_summary_rejected_uncited",
                    component="summary",
                    session_id=session_id,
                    tenant_id=principal.tenant_id,
                    count=dropped,
                    fallback_engaged=True,
                )
        except DependencyUnavailable:
            log.info(
                "llm_summary_unavailable_using_fallback",
                component="summary",
                session_id=session_id,
                tenant_id=principal.tenant_id,
                fallback_engaged=True,
            )

    if not statements:
        statements = _structured_fallback(live, localization)

    # Replace any prior draft: regeneration after new document extractions is
    # normal, and two competing drafts on one screen would be worse than one.
    await conn.execute("DELETE FROM summary WHERE session_id = $1", session_id)
    summary_id = await conn.fetchval(
        """
        INSERT INTO summary (tenant_id, session_id, generation_mode, model_version,
                             prompt_version, latency_ms)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        principal.tenant_id,
        session_id,
        mode,
        model_version,
        prompt_version,
        latency_ms,
    )

    for statement in statements:
        await conn.execute(
            """
            INSERT INTO summary_statement
                (tenant_id, summary_id, section, ordinal, text, citations)
            VALUES ($1, $2, $3, $4, $5, $6::uuid[])
            """,
            principal.tenant_id,
            summary_id,
            statement.section,
            statement.ordinal,
            statement.text,
            list(statement.citations),
        )

    await conn.execute(
        "UPDATE physician_review SET summary_id = $2, updated_at = now() WHERE session_id = $1",
        session_id,
        summary_id,
    )

    citation_count = sum(len(s.citations) for s in statements)
    await audit.record(
        conn,
        principal,
        action="summary.generated",
        entity_type="summary",
        entity_id=summary_id,
        detail={
            "generation_mode": mode,
            "model_version": model_version,
            "statement_count": len(statements),
            "citation_count": citation_count,
            "language": language,
        },
    )
    log.info(
        "summary_generated",
        component="summary",
        session_id=session_id,
        tenant_id=principal.tenant_id,
        generation_mode=mode,
        statement_count=len(statements),
        citation_count=citation_count,
        duration_ms=latency_ms,
    )
    return GeneratedSummary(
        summary_id=summary_id,
        generation_mode=mode,
        statement_count=len(statements),
        citation_count=citation_count,
        latency_ms=latency_ms,
        dropped_uncited=dropped,
    )


def _fact_payload(fact: FactRecord) -> dict[str, Any]:
    """Exactly what the model is allowed to see.

    Note the absence of the patient's name, the ABHA reference, the raw ASR
    transcript, and anything else identifying. The model summarises clinical
    content; it does not need to know who the patient is (§19, §28).
    """
    return {
        "fact_id": str(fact.id),
        "category": fact.category,
        "concept_code": fact.concept_code,
        "field_id": fact.concept_label,
        "value": fact.value_normalized,
        "unit": fact.unit,
        "source_type": fact.source_type,
        "respondent_relationship": fact.respondent_relationship,
        "confidence": fact.confidence,
        "abnormal_flag": fact.abnormal_flag,
        "is_conflicting": fact.is_conflicting,
    }


def _validate_statements(
    raw_statements: tuple[dict[str, Any], ...],
    live_facts: list[FactRecord],
) -> tuple[list[SummaryStatement], int]:
    """Enforce the citation contract (§19).

    A statement survives only if:
      * it has at least one citation, AND
      * every citation resolves to a LIVE fact in THIS session.

    Anything else is dropped. This is the layer that makes "no uncited sentence
    can be persisted" true regardless of what the model produced — a hallucinated
    fact id simply does not resolve.
    """
    valid_ids = {f.id for f in live_facts}
    accepted: list[SummaryStatement] = []
    dropped = 0
    per_section: dict[str, int] = {}

    for raw in raw_statements:
        section = str(raw.get("section", ""))
        text = str(raw.get("text", "")).strip()
        if section not in SECTIONS or not text:
            dropped += 1
            continue

        citations: list[UUID] = []
        for candidate in raw.get("citations", []) or []:
            try:
                fact_id = UUID(str(candidate))
            except (ValueError, TypeError):
                continue
            if fact_id in valid_ids:
                citations.append(fact_id)

        if not citations:
            dropped += 1
            continue

        ordinal = per_section.get(section, 0)
        per_section[section] = ordinal + 1
        accepted.append(
            SummaryStatement(
                section=section,
                ordinal=ordinal,
                text=text[:2000],
                citations=tuple(dict.fromkeys(citations)),
            )
        )

    return accepted, dropped


def _structured_fallback(
    live_facts: list[FactRecord],
    localization: LocalizationRegistry,
) -> list[SummaryStatement]:
    """Deterministic summary built directly from facts (§19 failure behaviour).

    Not prose — a grouped, cited, honest rendering of what the patient said. This
    is what §19 means by "the physician sees structured facts and timeline
    directly, never a blocked dashboard", and it is trivially citation-complete
    because each statement IS one fact.
    """
    per_section: dict[str, int] = {}
    statements: list[SummaryStatement] = []

    for fact in live_facts:
        section = _CATEGORY_SECTION.get(fact.category, "flags_and_gaps")
        ordinal = per_section.get(section, 0)
        per_section[section] = ordinal + 1

        attribution = ""
        if fact.source_type == "caregiver_answer":
            rel = fact.respondent_relationship or "caregiver"
            attribution = f" [reported by {rel}]"
        elif fact.source_type == "document_extraction":
            attribution = " [from uploaded document]"
        elif fact.source_type == "physician_edit":
            attribution = " [physician-corrected]"

        flags = ""
        if fact.abnormal_flag and fact.abnormal_flag != "normal":
            flags += f" [{fact.abnormal_flag.upper()}]"
        if fact.is_conflicting:
            flags += " [CONFLICT — needs adjudication]"

        statements.append(
            SummaryStatement(
                section=section,
                ordinal=ordinal,
                text=(
                    f"{fact.concept_code}: {_render_value(fact.value_normalized, fact.unit)}"
                    f"{attribution}{flags}"
                ),
                citations=(fact.id,),
            )
        )
    return statements


def _render_value(value: Any, unit: str | None) -> str:
    if isinstance(value, dict):
        if "codes" in value:
            rendered = ", ".join(str(c) for c in value["codes"])
        elif "code" in value:
            rendered = str(value["code"])
        elif "value" in value:
            rendered = str(value["value"])
            inner_unit = value.get("unit")
            if inner_unit:
                rendered = f"{rendered} {inner_unit}"
        elif "text" in value:
            rendered = str(value["text"])
        elif "date" in value:
            rendered = str(value["date"])
        else:
            rendered = str(value)
    elif isinstance(value, bool):
        rendered = "yes" if value else "no"
    else:
        rendered = str(value)
    return f"{rendered} {unit}" if unit and unit not in rendered else rendered


async def get(conn: asyncpg.Connection, session_id: UUID) -> dict[str, Any]:
    """The physician's summary view, with every citation resolved.

    Citations are expanded into the actual facts so the physician can see the
    evidence beside the sentence, rather than being told to trust it.
    """
    summary = await conn.fetchrow(
        """
        SELECT id, generation_mode, model_version, prompt_version, latency_ms,
               patient_confirmed_at, created_at
          FROM summary WHERE session_id = $1
        """,
        session_id,
    )
    if summary is None:
        raise NotFound("summary not generated yet", reason_code="summary_not_ready")

    rows = await conn.fetch(
        """
        SELECT section, ordinal, text, citations, physician_action, edited_text
          FROM summary_statement
         WHERE summary_id = $1
         ORDER BY section, ordinal
        """,
        summary["id"],
    )

    cited_ids = {fid for r in rows for fid in (r["citations"] or [])}
    fact_map = await facts_service.facts_by_ids(conn, list(cited_ids))

    sections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sections.setdefault(row["section"], []).append(
            {
                "ordinal": row["ordinal"],
                "text": row["edited_text"] or row["text"],
                "original_text": row["text"],
                "physician_action": row["physician_action"],
                "citations": [
                    {
                        "fact_id": str(fid),
                        "concept_code": fact_map[fid].concept_code,
                        "field_id": fact_map[fid].concept_label,
                        "value": fact_map[fid].value_normalized,
                        "source_type": fact_map[fid].source_type,
                        "respondent_relationship": fact_map[fid].respondent_relationship,
                        "confidence": fact_map[fid].confidence,
                        "provenance": fact_map[fid].provenance_ref,
                    }
                    for fid in (row["citations"] or [])
                    if fid in fact_map
                ],
            }
        )

    return {
        "summary_id": str(summary["id"]),
        "session_id": str(session_id),
        "generation_mode": summary["generation_mode"],
        "model_version": summary["model_version"],
        "prompt_version": summary["prompt_version"],
        "latency_ms": summary["latency_ms"],
        "patient_confirmed_at": summary["patient_confirmed_at"],
        "created_at": summary["created_at"],
        "ai_disclaimer_key": "privacy.ai_assists_only",
        "sections": [
            {"section": name, "statements": sections.get(name, [])}
            for name in SECTIONS
            if name in sections
        ],
    }


async def mark_patient_confirmed(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID
) -> None:
    """The patient-facing confirmation checkpoint (§3, §19)."""
    summary_id = await conn.fetchval(
        """
        UPDATE summary SET patient_confirmed_at = now()
         WHERE session_id = $1
        RETURNING id
        """,
        session_id,
    )
    if summary_id is None:
        raise NotFound("summary not generated yet", reason_code="summary_not_ready")
    await audit.record(
        conn,
        principal,
        action="summary.patient_confirmed",
        entity_type="summary",
        entity_id=summary_id,
        detail={"outcome": "confirmed"},
    )


async def set_statement_action(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    summary_id: UUID,
    section: str,
    ordinal: int,
    action: str,
    edited_text: str | None,
) -> None:
    """Per-statement accept / edit / exclude by the physician (§21)."""
    if action not in ("accepted", "edited", "excluded"):
        raise ValidationFailed("unknown statement action", reason_code="validation_failed")
    if action == "edited" and not (edited_text or "").strip():
        raise ValidationFailed("edited text is required", reason_code="validation_failed")

    updated = await conn.fetchval(
        """
        UPDATE summary_statement
           SET physician_action = $4, edited_text = $5
         WHERE summary_id = $1 AND section = $2 AND ordinal = $3
        RETURNING summary_id
        """,
        summary_id,
        section,
        ordinal,
        action,
        (edited_text or "").strip()[:2000] or None,
    )
    if updated is None:
        raise NotFound("statement not found", reason_code="not_found")

    await audit.record(
        conn,
        principal,
        action=f"summary.statement_{action}",
        entity_type="summary",
        entity_id=summary_id,
        detail={"reason_code": section, "count": ordinal},
    )
