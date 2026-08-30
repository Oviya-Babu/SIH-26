"""Session orchestration — the interactive interview loop (CLAUDE.md §3, §10, §14).

This is the synchronous, same-transaction core of the product:

    answer → validate/normalize → persist answer → write clinical fact
           → evaluate red flags → create alerts → recompute completeness
           → next question

All of it in ONE transaction, with the audit rows, because a patient's answer and
the safety decision about it must commit or roll back together (§31, §49).

[RED LINE §50] The interactive loop never touches RabbitMQ. Only document
processing, notification and integration relay are async.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.db import Principal, to_jsonb
from medikiosk.errors import Conflict, NotFound, SessionSealed, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.clinical_facts import service as facts
from medikiosk.modules.clinical_facts.service import (
    FactInput,
    SourceType,
    VerificationStatus,
)
from medikiosk.modules.clinical_protocol import engine
from medikiosk.modules.clinical_protocol.engine import (
    AnswerRecord,
    AnswerValidationError,
    ConfidenceVerdict,
    SessionState,
    SkipReason,
    Thresholds,
)
from medikiosk.modules.clinical_protocol.model import Field, Protocol, UnknownFieldError
from medikiosk.modules.consent import service as consent_service
from medikiosk.modules.consent.service import Purpose
from medikiosk.modules.localization.registry import LocalizationRegistry
from medikiosk.modules.triage import service as triage_service
from medikiosk.modules.triage.red_flag_engine import RuleSet
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

# Statuses in which the kiosk may still accept answers.
ANSWERABLE_STATUSES = ("in_progress", "escalated_to_staff", "awaiting_confirmation")


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    id: UUID
    tenant_id: UUID
    patient_id: UUID
    department_id: UUID
    protocol_family: str
    protocol_version: str
    language: str
    status: str
    respondent_type: str
    caregiver_auth_id: UUID | None
    fast_path_active: bool
    completeness: float
    review_status: str | None


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """What the kiosk needs to render after an answer."""

    accepted: bool
    verdict: ConfidenceVerdict
    fact_id: UUID | None
    completeness: float
    fast_path_engaged: bool
    escalated: bool
    alert_count: int
    session_status: str
    next_field_id: str | None


async def create_session(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    department_id: UUID,
    device_id: UUID | None,
    protocol_family: str,
    protocol_version: str,
    language: str,
    respondent_type: str,
    caregiver_auth_id: UUID | None,
) -> SessionSnapshot:
    """Start an interview.

    Internal consent for staff access is checked here, not later: §7.2 makes
    internal consent the gate on everything MediKiosk does, and starting an
    interview whose output could never reach a physician would waste the
    patient's time.
    """
    state = await consent_service.current_state(conn, patient_id)
    state.require(Purpose.STAFF_ACCESS)

    if respondent_type not in ("patient", "caregiver", "staff"):
        raise ValidationFailed("unknown respondent type", reason_code="validation_failed")
    if respondent_type == "caregiver" and caregiver_auth_id is None:
        raise ValidationFailed(
            "a caregiver respondent requires an authorization",
            reason_code="caregiver_ack_required",
        )

    # One live session per patient per department: a second kiosk start would
    # split the same visit across two records the physician then has to reconcile.
    existing = await conn.fetchval(
        """
        SELECT id FROM session
         WHERE patient_id = $1 AND department_id = $2
           AND status IN ('in_progress', 'escalated_to_staff', 'awaiting_confirmation')
         LIMIT 1
        """,
        patient_id,
        department_id,
    )
    if existing is not None:
        raise Conflict(
            "an interview is already in progress for this patient",
            reason_code="session_already_active",
            detail={"session_id": str(existing)},
        )

    row = await conn.fetchrow(
        """
        INSERT INTO session (tenant_id, patient_id, department_id, device_id,
                             protocol_family, protocol_version, language,
                             respondent_type, caregiver_auth_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        principal.tenant_id,
        patient_id,
        department_id,
        device_id,
        protocol_family,
        protocol_version,
        language,
        respondent_type,
        caregiver_auth_id,
    )

    # The review record is created up front, in state 'draft', so the physician
    # workflow has a single row to transition and the §21 state machine owns the
    # lifecycle from the first moment rather than being bolted on at submission.
    await conn.execute(
        """
        INSERT INTO physician_review (tenant_id, session_id, status)
        VALUES ($1, $2, 'draft')
        ON CONFLICT (session_id) DO NOTHING
        """,
        principal.tenant_id,
        row["id"],
    )

    await audit.record(
        conn,
        principal,
        action="session.created",
        entity_type="session",
        entity_id=row["id"],
        detail={
            "protocol_family": protocol_family,
            "protocol_version": protocol_version,
            "language": language,
            "respondent_type": respondent_type,
        },
    )
    log.info(
        "session_created",
        component="session",
        session_id=row["id"],
        tenant_id=principal.tenant_id,
        protocol_family=protocol_family,
        protocol_version=protocol_version,
        language=language,
        respondent_type=respondent_type,
    )
    return _snapshot(row, review_status="draft")


async def get_snapshot(conn: asyncpg.Connection, session_id: UUID) -> SessionSnapshot:
    row = await conn.fetchrow(
        """
        SELECT s.*, pr.status AS review_status
          FROM session s
          LEFT JOIN physician_review pr ON pr.session_id = s.id
         WHERE s.id = $1
        """,
        session_id,
    )
    if row is None:
        raise NotFound("session not found", reason_code="not_found")
    return _snapshot(row, review_status=row["review_status"])


async def load_state(conn: asyncpg.Connection, session_id: UUID) -> SessionState:
    """Rebuild the engine's view of the interview from persisted answers.

    Deriving state from the answer stream rather than caching it means a kiosk
    reload, a network drop, or a staff takeover all resume from exactly the same
    place — there is no second source of truth to disagree.
    """
    rows = await conn.fetch(
        """
        SELECT field_id, value_normalized, confidence, confirmed, skip_reason
          FROM session_answer
         WHERE session_id = $1 AND superseded_by IS NULL
        """,
        session_id,
    )
    fast_path = await conn.fetchval(
        "SELECT fast_path_active FROM session WHERE id = $1", session_id
    )
    answers = {
        r["field_id"]: AnswerRecord(
            field_id=r["field_id"],
            value=r["value_normalized"],
            confidence=float(r["confidence"]),
            confirmed=r["confirmed"],
            skip_reason=SkipReason(r["skip_reason"]) if r["skip_reason"] else None,
        )
        for r in rows
    }
    return SessionState(answers=answers, fast_path_active=bool(fast_path))


@dataclass(frozen=True, slots=True)
class RenderedQuestion:
    field_id: str
    concept_code: str
    category: str
    group: str
    group_label: str
    value_type: str
    widget: str
    required: bool
    confirm_back: bool
    voice_prompt: str
    touch_label: str
    help: str | None
    options: list[dict[str, Any]]
    unit_labels: dict[str, str]
    validation: dict[str, Any]
    language: str
    # Progress affordances for a first-time or low-literacy user (§1).
    answered_count: int
    required_count: int
    completeness: float
    group_progress: list[dict[str, Any]]
    fast_path_active: bool


def render_question(
    protocol: Protocol,
    localization: LocalizationRegistry,
    state: SessionState,
    field: Field,
    language: str,
) -> RenderedQuestion:
    """Turn a language-neutral field into what the kiosk shows and speaks.

    This is the ONLY place language enters the interview path. The engine chose
    ``field`` without seeing a single localized string.
    """
    rendered = localization.render_field(protocol, field, language)
    required = engine.required_fields(protocol, state)
    answered = state.answered_ids()

    return RenderedQuestion(
        field_id=field.id,
        concept_code=field.concept_code,
        category=field.category,
        group=field.group,
        group_label=localization.group_label(protocol, field.group, language),
        value_type=str(field.value_type),
        widget=str(field.widget),
        required=field.required or (state.fast_path_active and field.ample),
        confirm_back=field.confirm_back,
        voice_prompt=rendered.voice_prompt,
        touch_label=rendered.touch_label,
        help=rendered.help,
        options=[
            {
                "value": o.value,
                "label": o.label,
                "icon": o.icon,
                "help": o.help,
                "critical": next((x.critical for x in field.options if x.value == o.value), False),
                "exclusive": next(
                    (x.exclusive for x in field.options if x.value == o.value), False
                ),
            }
            for o in rendered.options
        ],
        unit_labels=dict(rendered.unit_labels),
        validation={
            "min": field.validation.min,
            "max": field.validation.max,
            "max_length": field.validation.max_length,
            "units": list(field.validation.units),
        },
        language=language,
        answered_count=sum(1 for fid in required if fid in answered),
        required_count=len(required),
        completeness=engine.completeness(protocol, state),
        group_progress=[dict(row) for row in engine.group_progress(protocol, state)],
        fast_path_active=state.fast_path_active,
    )


async def submit_answer(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session: SessionSnapshot,
    protocol: Protocol,
    ruleset: RuleSet,
    thresholds: Thresholds,
    field_id: str,
    raw_value: Any,
    input_method: str,
    confidence: float,
    confirmed: bool,
    skip_reason: SkipReason | None,
    respondent_id: UUID,
    respondent_relationship: str | None,
    asr_transcript: str | None = None,
) -> AnswerOutcome:
    """The whole answer transaction (§3, §14, §31).

    Returns before the caller pushes the nurse notification, which happens after
    commit — announcing an alert that then rolls back would send a nurse to a
    patient whose answer was never recorded.
    """
    if session.review_status == "exported":
        raise SessionSealed("this session has been exported", reason_code="session_sealed_after_export")
    if session.status not in ANSWERABLE_STATUSES:
        raise Conflict(
            f"session is {session.status}", reason_code="session_not_answerable"
        )

    try:
        field = protocol.field_or_raise(field_id)
    except UnknownFieldError as exc:
        raise ValidationFailed("unknown field", reason_code="unknown_field") from exc

    state = await load_state(conn, session.id)

    # The engine decides what may be answered. Accepting an arbitrary field id
    # would let a client drive the interview out of protocol order, which is
    # exactly the determinism §10 requires us to preserve.
    in_scope = engine.in_scope_fields(protocol, state)
    if field_id not in in_scope:
        raise Conflict(
            "that question is not currently in scope",
            reason_code="field_out_of_scope",
        )

    if input_method not in ("voice", "touch", "text"):
        raise ValidationFailed("unknown input method", reason_code="validation_failed")
    if input_method == "voice":
        # Voice capture is a separately consentable purpose (§7.2).
        consent_state = await consent_service.current_state(conn, session.patient_id)
        consent_state.require(Purpose.VOICE_CAPTURE)

    # --- skip path ----------------------------------------------------------
    if skip_reason is not None:
        return await _record_skip(
            conn,
            principal,
            session=session,
            protocol=protocol,
            field=field,
            skip_reason=skip_reason,
            respondent_id=respondent_id,
            respondent_relationship=respondent_relationship,
            input_method=input_method,
        )

    # --- validate and gate --------------------------------------------------
    try:
        normalized = engine.validate_and_normalize(field, raw_value)
    except AnswerValidationError as exc:
        raise ValidationFailed(str(exc), reason_code=exc.reason_code) from exc

    verdict = engine.gate_confidence(field, confidence, thresholds)
    if verdict is ConfidenceVerdict.REJECT:
        # Below τ_low the value is not trustworthy enough to persist at all; the
        # kiosk re-prompts or falls back to touch (§10, §18.2).
        log.info(
            "answer_rejected_low_confidence",
            component="session",
            session_id=session.id,
            tenant_id=principal.tenant_id,
            field_id=field_id,
            confidence=confidence,
        )
        return AnswerOutcome(
            accepted=False,
            verdict=verdict,
            fact_id=None,
            completeness=engine.completeness(protocol, state),
            fast_path_engaged=state.fast_path_active,
            escalated=False,
            alert_count=0,
            session_status=session.status,
            next_field_id=field_id,
        )
    if verdict is ConfidenceVerdict.CONFIRM and not confirmed:
        # Hold the value out of the record until the respondent confirms it.
        return AnswerOutcome(
            accepted=False,
            verdict=verdict,
            fact_id=None,
            completeness=engine.completeness(protocol, state),
            fast_path_engaged=state.fast_path_active,
            escalated=False,
            alert_count=0,
            session_status=session.status,
            next_field_id=field_id,
        )

    # --- persist the answer -------------------------------------------------
    await _supersede_previous_answer(conn, session.id, field_id)
    answer_id = await conn.fetchval(
        """
        INSERT INTO session_answer
            (tenant_id, session_id, field_id, value_raw, value_normalized, input_method,
             confidence, confirmed, respondent_type, respondent_id, respondent_relationship)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        principal.tenant_id,
        session.id,
        field_id,
        asr_transcript,
        to_jsonb(normalized),
        input_method,
        confidence,
        confirmed or verdict is ConfidenceVerdict.ACCEPT,
        session.respondent_type,
        respondent_id,
        respondent_relationship,
    )

    # --- derive the clinical fact ------------------------------------------
    source_type = (
        SourceType.CAREGIVER_ANSWER
        if session.respondent_type == "caregiver"
        else SourceType.STAFF_ENTRY
        if session.respondent_type == "staff"
        else SourceType.PATIENT_ANSWER
    )
    concept = protocol.concept_or_raise(field.concept_code)
    fact = await facts.write(
        conn,
        principal,
        FactInput(
            session_id=session.id,
            patient_id=session.patient_id,
            category=field.category,
            concept_code=concept.code,
            concept_label=field.id,
            value_normalized=normalized,
            value_raw=asr_transcript,
            confidence=confidence,
            source_type=source_type,
            respondent_id=respondent_id,
            respondent_relationship=respondent_relationship,
            provenance_ref={
                "method": f"interview_{input_method}",
                "field_id": field_id,
                "answer_id": str(answer_id),
                "protocol_family": protocol.family,
                "protocol_version": protocol.version,
                "protocol_checksum": protocol.content_checksum,
                "confirmed": bool(confirmed or verdict is ConfidenceVerdict.ACCEPT),
            },
            verification_status=(
                VerificationStatus.PATIENT_CONFIRMED
                if confirmed
                else VerificationStatus.UNVERIFIED
            ),
            extra_audit={"field_id": field_id, "input_method": input_method},
        ),
    )

    # --- red flags, same transaction ---------------------------------------
    state = state.with_answer(
        AnswerRecord(
            field_id=field_id, value=normalized, confidence=confidence, confirmed=confirmed
        )
    )
    result = await triage_service.evaluate_and_persist(
        conn,
        principal,
        ruleset=ruleset,
        protocol=protocol,
        session_id=session.id,
        department_id=session.department_id,
        answers=state.answer_view(),
        trigger_field_id=field_id,
    )

    escalated = False
    if result.requires_fast_path and not session.fast_path_active:
        state = state.with_fast_path()
        escalated = True
        await _engage_fast_path(conn, principal, session=session, protocol=protocol, state=state)

    completeness = engine.completeness(protocol, state)
    next_field = engine.next_field(protocol, state)
    status = "escalated_to_staff" if (escalated or session.fast_path_active) else session.status

    await conn.execute(
        """
        UPDATE session
           SET completeness = $2, last_activity_at = now(), status = $3
         WHERE id = $1
        """,
        session.id,
        completeness,
        status,
    )

    return AnswerOutcome(
        accepted=True,
        verdict=verdict,
        fact_id=fact.id,
        completeness=completeness,
        fast_path_engaged=state.fast_path_active,
        escalated=escalated,
        alert_count=len(result.alerts),
        session_status=status,
        next_field_id=next_field.id if next_field else None,
    )


async def _record_skip(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session: SessionSnapshot,
    protocol: Protocol,
    field: Field,
    skip_reason: SkipReason,
    respondent_id: UUID,
    respondent_relationship: str | None,
    input_method: str,
) -> AnswerOutcome:
    """Record 'I do not know' / 'I prefer not to answer'.

    §14.4: this is stored as a distinct reason, never as an empty answer, so the
    physician's dashboard can tell "the patient did not know" apart from "we
    never asked".
    """
    if skip_reason is SkipReason.NOT_ASKED_DUE_TO_EMERGENCY_ESCALATION:
        raise ValidationFailed(
            "escalation skips are recorded by the system, not submitted",
            reason_code="validation_failed",
        )

    await _supersede_previous_answer(conn, session.id, field.id)
    await conn.execute(
        """
        INSERT INTO session_answer
            (tenant_id, session_id, field_id, value_normalized, input_method, confidence,
             confirmed, respondent_type, respondent_id, respondent_relationship, skip_reason)
        VALUES ($1, $2, $3, 'null'::jsonb, $4, 1.0, true, $5, $6, $7, $8)
        """,
        principal.tenant_id,
        session.id,
        field.id,
        input_method,
        session.respondent_type,
        respondent_id,
        respondent_relationship,
        str(skip_reason),
    )
    await audit.record(
        conn,
        principal,
        action="session.answer_skipped",
        entity_type="session",
        entity_id=session.id,
        detail={"field_id": field.id, "skip_reason": str(skip_reason)},
    )

    state = await load_state(conn, session.id)
    completeness = engine.completeness(protocol, state)
    next_field = engine.next_field(protocol, state)
    await conn.execute(
        "UPDATE session SET completeness = $2, last_activity_at = now() WHERE id = $1",
        session.id,
        completeness,
    )
    return AnswerOutcome(
        accepted=True,
        verdict=ConfidenceVerdict.ACCEPT,
        fact_id=None,
        completeness=completeness,
        fast_path_engaged=state.fast_path_active,
        escalated=False,
        alert_count=0,
        session_status=session.status,
        next_field_id=next_field.id if next_field else None,
    )


async def _engage_fast_path(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session: SessionSnapshot,
    protocol: Protocol,
    state: SessionState,
) -> None:
    """Switch to the AMPLE fast path (§14.1–14.5).

    1. Answered is never cleared — nothing is deleted here.
    2. R(state) becomes the AMPLE set — that is the engine's behaviour once
       ``fast_path_active`` is set.
    3. Completeness is recomputed against the reduced set by the caller.
    4. Every routine question still outstanding is marked
       ``not_asked_due_to_emergency_escalation``, so the physician never sees a
       gap and assumes the patient did not know.
    5. Session.status becomes ``escalated_to_staff``, not ``completed``.
    """
    routine_state = SessionState(answers=dict(state.answers), fast_path_active=False)
    outstanding = engine.remaining_required(protocol, routine_state)

    if outstanding:
        await conn.executemany(
            """
            INSERT INTO session_answer
                (tenant_id, session_id, field_id, value_normalized, input_method,
                 confidence, confirmed, respondent_type, respondent_id, skip_reason)
            VALUES ($1, $2, $3, 'null'::jsonb, 'touch', 1.0, false, 'staff', $4,
                    'not_asked_due_to_emergency_escalation')
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    principal.tenant_id,
                    session.id,
                    field_id,
                    principal.actor_id or session.patient_id,
                )
                for field_id in outstanding
            ],
        )

    await conn.execute(
        """
        UPDATE session
           SET fast_path_active = true,
               fast_path_activated_at = now(),
               status = 'escalated_to_staff'
         WHERE id = $1
        """,
        session.id,
    )
    await audit.record(
        conn,
        principal,
        action="session.fast_path_engaged",
        entity_type="session",
        entity_id=session.id,
        detail={
            "fast_path": True,
            "count": len(outstanding),
            "next_status": "escalated_to_staff",
            "reason_code": "red_flag_critical",
        },
    )
    log.info(
        "fast_path_engaged",
        component="session",
        session_id=session.id,
        tenant_id=principal.tenant_id,
        count=len(outstanding),
    )


async def _supersede_previous_answer(
    conn: asyncpg.Connection, session_id: UUID, field_id: str
) -> None:
    """Re-answering supersedes rather than overwrites.

    A patient correcting themselves is information, not noise: the original and
    the correction are both attributable.
    """
    await conn.execute(
        """
        UPDATE session_answer
           SET superseded_by = id
         WHERE session_id = $1 AND field_id = $2 AND superseded_by IS NULL
        """,
        session_id,
        field_id,
    )


async def next_question(
    conn: asyncpg.Connection,
    *,
    session: SessionSnapshot,
    protocol: Protocol,
    localization: LocalizationRegistry,
) -> RenderedQuestion | None:
    state = await load_state(conn, session.id)
    field = engine.next_field(protocol, state)
    if field is None:
        return None
    return render_question(protocol, localization, state, field, session.language)


async def set_language(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
    language: str,
) -> None:
    """Change the interview language mid-session.

    Purely presentational: no clinical state changes, because answers are stored
    as language-neutral codes. A patient can switch after realising the kiosk
    started in the wrong language, and keep every answer already given.
    """
    await conn.execute(
        "UPDATE session SET language = $2, last_activity_at = now() WHERE id = $1",
        session_id,
        language,
    )
    await audit.record(
        conn,
        principal,
        action="session.language_changed",
        entity_type="session",
        entity_id=session_id,
        detail={"language": language},
    )


async def mark_awaiting_confirmation(
    conn: asyncpg.Connection, principal: Principal, session_id: UUID
) -> None:
    await conn.execute(
        """
        UPDATE session SET status = 'awaiting_confirmation', last_activity_at = now()
         WHERE id = $1 AND status = 'in_progress'
        """,
        session_id,
    )
    await audit.record(
        conn,
        principal,
        action="session.awaiting_confirmation",
        entity_type="session",
        entity_id=session_id,
        detail={"next_status": "awaiting_confirmation"},
    )


async def complete_session(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
) -> str:
    """Finish the kiosk interview.

    An escalated session ends as ``escalated_to_staff``, never ``completed``
    (§14.5): staff took over physically, and marking it complete would claim an
    interview finished that in fact was cut short.
    """
    row = await conn.fetchrow(
        "SELECT status, fast_path_active FROM session WHERE id = $1", session_id
    )
    if row is None:
        raise NotFound("session not found", reason_code="not_found")

    final = "escalated_to_staff" if row["fast_path_active"] else "completed"
    await conn.execute(
        """
        UPDATE session
           SET status = $2, submitted_at = now(), last_activity_at = now()
         WHERE id = $1
        """,
        session_id,
        final,
    )
    await audit.record(
        conn,
        principal,
        action="session.submitted",
        entity_type="session",
        entity_id=session_id,
        detail={"previous_status": row["status"], "next_status": final},
    )
    return final


async def abandon_idle_sessions(
    conn: asyncpg.Connection, principal: Principal, *, idle_seconds: int
) -> list[UUID]:
    """Idle-timeout cleanup (§8).

    The visible manifestation is the kiosk resetting to the start screen between
    patients; this is the server side of the same rule.
    """
    rows = await conn.fetch(
        """
        UPDATE session
           SET status = 'abandoned'
         WHERE status = 'in_progress'
           AND last_activity_at < now() - make_interval(secs => $1)
        RETURNING id
        """,
        idle_seconds,
    )
    for row in rows:
        await audit.record(
            conn,
            principal,
            action="session.abandoned_idle",
            entity_type="session",
            entity_id=row["id"],
            detail={"next_status": "abandoned", "reason_code": "idle_timeout"},
        )
    return [r["id"] for r in rows]


async def answered_summary(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    """The answer stream with provenance, for the patient confirmation screen
    and the physician's provenance column (§13, §21)."""
    rows = await conn.fetch(
        """
        SELECT a.field_id, a.value_normalized, a.input_method, a.confidence,
               a.confirmed, a.skip_reason, a.respondent_type,
               a.respondent_relationship, a.created_at
          FROM session_answer a
         WHERE a.session_id = $1 AND a.superseded_by IS NULL
         ORDER BY a.created_at
        """,
        session_id,
    )
    return [dict(r) for r in rows]


def _snapshot(row, *, review_status: str | None) -> SessionSnapshot:
    return SessionSnapshot(
        id=row["id"],
        tenant_id=row["tenant_id"],
        patient_id=row["patient_id"],
        department_id=row["department_id"],
        protocol_family=row["protocol_family"],
        protocol_version=row["protocol_version"],
        language=row["language"],
        status=row["status"],
        respondent_type=row["respondent_type"],
        caregiver_auth_id=row["caregiver_auth_id"],
        fast_path_active=row["fast_path_active"],
        completeness=float(row["completeness"]),
        review_status=review_status,
    )
