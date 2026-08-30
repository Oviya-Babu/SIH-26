"""The interactive interview API (CLAUDE.md §49).

    POST /v1/sessions
    GET  /v1/sessions/{id}/next-question
    POST /v1/sessions/{id}/answers
    POST /v1/sessions/{id}/confirm
    POST /v1/sessions/{id}/submit

These are the latency-critical endpoints of §54. Everything here is synchronous
and same-transaction; nothing here touches RabbitMQ [RED LINE §50].
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field as PField

from medikiosk.deps import (
    Ctx,
    KioskPrincipal,
    SessionPrincipal,
    load_session_row,
    require,
    session_resource,
)
from medikiosk.errors import Forbidden, NotFound, ValidationFailed
from medikiosk.modules.caregiver import service as caregiver_service
from medikiosk.modules.clinical_facts import service as facts_service
from medikiosk.modules.clinical_protocol.engine import SkipReason
from medikiosk.modules.purge import service as purge_service
from medikiosk.modules.session import service as session_service
from medikiosk.modules.tenant import service as tenant_service
from medikiosk.modules.triage import service as triage_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.rbac import Capability

log = get_logger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["interview"])


class StartSessionRequest(BaseModel):
    patient_id: UUID
    # Department is offered by the kiosk; the device token constrains which
    # values are acceptable, so a client cannot pick an arbitrary department.
    department_id: UUID
    language: str = "en"
    respondent_type: Literal["patient", "caregiver"] = "patient"
    caregiver_auth_id: UUID | None = None


class StartSessionResponse(BaseModel):
    session_id: UUID
    session_token: str
    expires_in: int
    protocol_family: str
    protocol_version: str
    protocol_checksum: str
    language: str
    department: dict[str, Any]
    respondent_type: str
    respondent_label: str | None
    estimated_questions: int


@router.post("", response_model=StartSessionResponse)
async def start_session(
    ctx: Ctx, principal: KioskPrincipal, payload: StartSessionRequest
) -> StartSessionResponse:
    """Start an interview and mint the session-scoped token.

    §8: the device fixes the tenant, and if the device is bound to a department
    it fixes that too. A request asking for a different department is refused
    rather than honoured.
    """
    if principal.department_id is not None and payload.department_id != principal.department_id:
        raise Forbidden(
            "this kiosk is provisioned for a different department",
            reason_code="department_fixed_by_device",
        )

    language = ctx.localization.normalize(payload.language)

    async with ctx.db.transaction(principal) as conn:
        resolved = await tenant_service.resolve_protocol(
            conn, ctx.protocols, department_id=payload.department_id
        )

        respondent_label: str | None = None
        caregiver_auth_id = None
        if payload.respondent_type == "caregiver":
            if payload.caregiver_auth_id is None:
                raise ValidationFailed(
                    "a caregiver respondent requires an acknowledged authorization",
                    reason_code="caregiver_ack_required",
                )
            authorization = await caregiver_service.assert_may_respond(
                conn, payload.caregiver_auth_id, payload.patient_id
            )
            caregiver_auth_id = authorization.id
            respondent_label = (
                f"Reported by: {authorization.caregiver_name}, "
                f"relationship: {authorization.relationship}"
            )

        session = await session_service.create_session(
            conn,
            principal,
            patient_id=payload.patient_id,
            department_id=payload.department_id,
            device_id=principal.actor_id,
            protocol_family=resolved.family,
            protocol_version=resolved.version,
            language=language,
            respondent_type=payload.respondent_type,
            caregiver_auth_id=caregiver_auth_id,
        )

    token, claims = ctx.tokens.mint(
        "session",
        tenant_id=principal.tenant_id,
        ttl_seconds=ctx.settings.session_token_ttl_seconds,
        session_id=session.id,
        patient_id=payload.patient_id,
        department_id=payload.department_id,
        subject_role=(
            "caregiver_respondent" if payload.respondent_type == "caregiver" else "patient"
        ),
        caregiver_auth_id=caregiver_auth_id,
        actor_id=payload.patient_id,
    )

    protocol = resolved.protocol
    return StartSessionResponse(
        session_id=session.id,
        session_token=token,
        expires_in=claims.expires_at - claims.issued_at,
        protocol_family=resolved.family,
        protocol_version=resolved.version,
        protocol_checksum=protocol.content_checksum[:12],
        language=language,
        department={
            "id": str(resolved.department.id),
            "code": resolved.department.code,
            "display_name": resolved.department.display_name,
        },
        respondent_type=payload.respondent_type,
        respondent_label=respondent_label,
        estimated_questions=sum(1 for f in protocol.fields.values() if f.required),
    )


class NextQuestionResponse(BaseModel):
    complete: bool
    session_status: str
    fast_path_active: bool
    completeness: float
    question: dict[str, Any] | None
    # Present when a critical red flag has escalated the session: the kiosk
    # shows the calm escalation screen (§14) alongside the AMPLE questions.
    escalation: dict[str, Any] | None


@router.get("/{session_id}/next-question", response_model=NextQuestionResponse)
async def next_question(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_READ_OWN, "read", tier="session"))],
) -> NextQuestionResponse:
    """``NextField(session)`` — deterministic, then localized (§10)."""
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        session = await session_service.get_snapshot(conn, session_id)
        protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)
        question = await session_service.next_question(
            conn, session=session, protocol=protocol, localization=ctx.localization
        )
        state = await session_service.load_state(conn, session_id)

    escalation = None
    if session.fast_path_active:
        escalation = {
            # Only i18n keys cross this boundary — the kiosk resolves the words,
            # so no clinical rationale can reach the patient's screen (§14).
            "message_key": "escalation.body",
            "spoken_key": "escalation.body_spoken",
            "remaining_key": "escalation.few_more_questions",
        }

    return NextQuestionResponse(
        complete=question is None,
        session_status=session.status,
        fast_path_active=session.fast_path_active,
        completeness=session.completeness,
        question=_question_payload(question) if question else None,
        escalation=escalation,
    )


class AnswerRequest(BaseModel):
    field_id: str
    value: Any = None
    input_method: Literal["voice", "touch", "text"] = "touch"
    # ASR confidence, or 1.0 for a touch answer — a tap is not a guess.
    confidence: float = PField(default=1.0, ge=0.0, le=1.0)
    confirmed: bool = False
    # 'not_answered' is never submitted; the patient chooses declined/unsure.
    skip_reason: Literal["patient_declined", "patient_unsure"] | None = None
    # The raw transcript, retained as value_raw for provenance. The AUDIO is
    # never persisted (§17.3, §38).
    asr_transcript: str | None = None


class AnswerResponse(BaseModel):
    accepted: bool
    verdict: str
    completeness: float
    fast_path_engaged: bool
    escalated: bool
    session_status: str
    next_question: dict[str, Any] | None
    complete: bool
    escalation: dict[str, Any] | None
    # Set when the verdict is 'confirm': the kiosk reads this back before the
    # value enters the record (§10).
    confirm_prompt: str | None


@router.post("/{session_id}/answers", response_model=AnswerResponse)
async def submit_answer(
    ctx: Ctx,
    session_id: UUID,
    payload: AnswerRequest,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_ANSWER, "answer", tier="session"))],
) -> AnswerResponse:
    """One answer: validate → persist → fact → red flags → next question.

    All in one transaction (§31). The nurse notification is pushed AFTER the
    commit, so an alert can never announce an answer that rolled back.
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    ruleset = ctx.ruleset
    notify = False
    department_id: UUID | None = None

    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        session = await session_service.get_snapshot(conn, session_id)
        protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)

        respondent_relationship = None
        if session.respondent_type == "caregiver" and session.caregiver_auth_id:
            authorization = await caregiver_service.assert_may_respond(
                conn, session.caregiver_auth_id, session.patient_id
            )
            respondent_relationship = authorization.relationship

        outcome = await session_service.submit_answer(
            conn,
            principal,
            session=session,
            protocol=protocol,
            ruleset=ruleset,
            thresholds=ctx.thresholds,
            field_id=payload.field_id,
            raw_value=payload.value,
            input_method=payload.input_method,
            confidence=payload.confidence,
            confirmed=payload.confirmed,
            skip_reason=SkipReason(payload.skip_reason) if payload.skip_reason else None,
            respondent_id=principal.patient_id or session.patient_id,
            respondent_relationship=respondent_relationship,
            asr_transcript=payload.asr_transcript,
        )

        # Re-read state to render the next question with fresh progress numbers.
        session = await session_service.get_snapshot(conn, session_id)
        question = await session_service.next_question(
            conn, session=session, protocol=protocol, localization=ctx.localization
        )
        confirm_prompt = None
        if outcome.verdict == "confirm":
            field = protocol.field_or_raise(payload.field_id)
            rendered = ctx.localization.render_field(protocol, field, session.language)
            confirm_prompt = rendered.confirm_prompt or rendered.touch_label

        notify = outcome.alert_count > 0 or outcome.escalated
        department_id = session.department_id

    # --- after commit --------------------------------------------------------
    if notify and department_id is not None:
        async with ctx.db.readonly(principal) as conn:
            delivered = await triage_service.notify_new_alerts(
                conn,
                tenant_id=principal.tenant_id,
                session_id=session_id,
                department_id=department_id,
            )
        log.info(
            "alert_pushed",
            component="triage",
            session_id=session_id,
            tenant_id=principal.tenant_id,
            count=delivered,
        )

    escalation = None
    if outcome.fast_path_engaged:
        escalation = {
            "message_key": "escalation.body",
            "spoken_key": "escalation.body_spoken",
            "remaining_key": "escalation.few_more_questions",
        }

    return AnswerResponse(
        accepted=outcome.accepted,
        verdict=str(outcome.verdict),
        completeness=outcome.completeness,
        fast_path_engaged=outcome.fast_path_engaged,
        escalated=outcome.escalated,
        session_status=outcome.session_status,
        next_question=_question_payload(question) if question else None,
        complete=question is None,
        escalation=escalation,
        confirm_prompt=confirm_prompt,
    )


class LanguageRequest(BaseModel):
    language: str


@router.post("/{session_id}/language")
async def change_language(
    ctx: Ctx,
    session_id: UUID,
    payload: LanguageRequest,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_ANSWER, "answer", tier="session"))],
) -> dict[str, Any]:
    """Switch language mid-interview without losing a single answer.

    Answers are stored as language-neutral codes, so this is purely a
    presentation change — which is the whole point of keeping the engine
    language-neutral.
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")
    language = ctx.localization.normalize(payload.language)

    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        await session_service.set_language(
            conn, principal, session_id=session_id, language=language
        )
        session = await session_service.get_snapshot(conn, session_id)
        protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)
        question = await session_service.next_question(
            conn, session=session, protocol=protocol, localization=ctx.localization
        )

    return {
        "language": language,
        "question": _question_payload(question) if question else None,
    }


class ConfirmationResponse(BaseModel):
    session_id: UUID
    completeness: float
    facts: list[dict[str, Any]]
    answers: list[dict[str, Any]]


@router.get("/{session_id}/confirmation", response_model=ConfirmationResponse)
async def confirmation_view(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_READ_OWN, "read", tier="session"))],
) -> ConfirmationResponse:
    """What the patient reviews before submitting (§3 step N, §19).

    Facts are shown with their provenance labels so a caregiver-reported answer
    is visibly attributed to the caregiver — never presented as the patient's own
    words (§6).
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        session = await session_service.get_snapshot(conn, session_id)
        protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)
        current = await facts_service.current_facts(conn, session_id)
        answers = await session_service.answered_summary(conn, session_id)

    rendered_facts = []
    for fact in current:
        field = protocol.fields.get(fact.concept_label)
        label = (
            ctx.localization.render_field(protocol, field, session.language).touch_label
            if field
            else fact.concept_code
        )
        rendered_facts.append(
            {
                "fact_id": str(fact.id),
                "field_id": fact.concept_label,
                "label": label,
                "category": fact.category,
                "value": fact.value_normalized,
                "source_type": fact.source_type,
                "respondent_relationship": fact.respondent_relationship,
                "confidence": fact.confidence,
            }
        )

    return ConfirmationResponse(
        session_id=session_id,
        completeness=session.completeness,
        facts=rendered_facts,
        answers=[
            {
                "field_id": a["field_id"],
                "input_method": a["input_method"],
                "confirmed": a["confirmed"],
                "skip_reason": a["skip_reason"],
                "respondent_type": a["respondent_type"],
            }
            for a in answers
        ],
    )


class SubmitResponse(BaseModel):
    session_id: UUID
    status: str
    completeness: float
    transient_purged: bool
    purged_keys: int
    summary_pending: bool


@router.post("/{session_id}/submit", response_model=SubmitResponse)
async def submit_session(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_CONFIRM, "confirm", tier="session"))],
) -> SubmitResponse:
    """Finish the interview.

    The synchronous transient purge of §38 happens HERE, inside the same
    transaction as the status change — code-enforced, not scheduled, and provable
    afterwards via ``session.transient_purged_at``.
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    store = getattr(ctx, "transient_store", None)

    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        status = await session_service.complete_session(
            conn, principal, session_id=session_id
        )
        await purge_service.schedule_document_retention(conn, principal, session_id=session_id)
        purge_result = await purge_service.purge_session_transients(
            conn, principal, store, session_id=session_id
        )
        session = await session_service.get_snapshot(conn, session_id)

    return SubmitResponse(
        session_id=session_id,
        status=status,
        completeness=session.completeness,
        transient_purged=True,
        purged_keys=purge_result.transient_keys_removed,
        # The summary is generated asynchronously and never blocks the patient
        # (§54): they may leave the kiosk immediately.
        summary_pending=True,
    )


@router.get("/{session_id}/status")
async def session_status(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_READ_OWN, "read", tier="session"))],
) -> dict[str, Any]:
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")
    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        session = await session_service.get_snapshot(conn, session_id)
        documents = await conn.fetch(
            """
            SELECT id, processing_status, quality_status, capture_path, created_at
              FROM document WHERE session_id = $1 ORDER BY created_at
            """,
            session_id,
        )
    return {
        "session_id": str(session_id),
        "status": session.status,
        "completeness": session.completeness,
        "fast_path_active": session.fast_path_active,
        "language": session.language,
        "documents": [
            {
                "document_id": str(d["id"]),
                "processing_status": d["processing_status"],
                "quality_status": d["quality_status"],
                "capture_path": d["capture_path"],
            }
            for d in documents
        ],
    }


def _question_payload(question) -> dict[str, Any]:
    if question is None:
        raise NotFound("no question available", reason_code="not_found")
    return {
        "field_id": question.field_id,
        "concept_code": question.concept_code,
        "category": question.category,
        "group": question.group,
        "group_label": question.group_label,
        "value_type": question.value_type,
        "widget": question.widget,
        "required": question.required,
        "confirm_back": question.confirm_back,
        "voice_prompt": question.voice_prompt,
        "touch_label": question.touch_label,
        "help": question.help,
        "options": question.options,
        "unit_labels": question.unit_labels,
        "validation": question.validation,
        "language": question.language,
        "progress": {
            "answered": question.answered_count,
            "required": question.required_count,
            "completeness": question.completeness,
            "groups": question.group_progress,
            "fast_path_active": question.fast_path_active,
        },
    }
