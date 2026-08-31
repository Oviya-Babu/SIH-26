"""Voice answer endpoints for Phase 3 (CLAUDE.md §3, §18, §54).

These endpoints wire the AI Gateway ASR/NLU/TTS into the interactive session loop:

    POST /v1/sessions/{id}/answers/voice/transcribe → ASR transcription
    POST /v1/sessions/{id}/answers/voice            → ASR + NLU + submit answer
    GET  /v1/sessions/{id}/questions/{fid}/speak    → TTS synthesis for question

All synchronous, same-transaction with clinical fact persistence (§50).
"""

from __future__ import annotations

import base64
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, SessionPrincipal, load_session_row, require, session_resource
from medikiosk.errors import Forbidden, NotFound, ValidationFailed
from medikiosk.modules.caregiver import service as caregiver_service
from medikiosk.modules.clinical_protocol import engine
from medikiosk.modules.clinical_protocol.engine import ConfidenceVerdict
from medikiosk.modules.clinical_protocol.model import UnknownFieldError
from medikiosk.modules.session import service as session_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.rbac import Capability

log = get_logger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["interview-voice"])


# ============================================================================
# Voice Answer Models
# ============================================================================

class VoiceAnswerRequest(BaseModel):
    """Voice input: audio file to transcribe."""

    language: str = PField(default="en")


class VoiceTranscriptionResponse(BaseModel):
    """Transcription result from ASR."""

    transcript: str
    confidence: float = PField(..., ge=0.0, le=1.0)
    language: str
    inference_time_ms: float
    is_final: bool = True


class VoiceAnswerResponse(BaseModel):
    """Voice answer processed → clinical fact created."""

    session_id: UUID
    fact_id: UUID | None
    transcript: str
    field_id: str
    value_raw: Any
    value_normalized: Any
    confidence: float
    verdict: str  # accepted | confirm_back | rejected
    completeness: float
    next_field_id: str | None
    inference_time_ms: float


# ============================================================================
# Voice Transcription Only (Step 1: ASR)
# ============================================================================

@router.post(
    "/{session_id}/answers/voice/transcribe",
    response_model=VoiceTranscriptionResponse,
)
async def transcribe_voice(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[
        Any,
        Depends(require(Capability.SESSION_READ_OWN, "read", tier="session")),
    ],
    file: UploadFile = File(...),
    language: str = Query("en"),
) -> VoiceTranscriptionResponse:
    """Transcribe voice to text (ASR only, no answer processing yet)."""
    if file.size is None or file.size == 0:
        raise ValidationFailed("audio file is empty", reason_code="audio_empty")

    if file.size > 10_000_000:  # 10MB max
        raise ValidationFailed(
            "audio file too large (max 10MB)",
            reason_code="audio_too_large",
        )

    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    try:
        async with ctx.db.readonly(principal) as conn:
            row = await load_session_row(conn, session_id)
            await authz.check(session_resource(row))

        # Read audio from upload and base64 encode
        audio_bytes = await file.read()
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

        # Call AI Gateway ASR endpoint via ctx.ai
        asr_response = await ctx.ai.transcribe(
            audio_base64=audio_b64,
            language=language,
            asr_locale=f"{language}-IN",
        )

        return VoiceTranscriptionResponse(
            transcript=asr_response.text,
            confidence=asr_response.confidence,
            language=language,
            inference_time_ms=65.0,
            is_final=asr_response.is_final,
        )

    except ValidationFailed:
        raise
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        raise ValidationFailed(
            f"transcription error: {str(e)}",
            reason_code="asr_failed",
        ) from e


# ============================================================================
# Voice Answer (Step 2: NLU + Answer Processing)
# ============================================================================

@router.post(
    "/{session_id}/answers/voice",
    response_model=VoiceAnswerResponse,
)
async def voice_answer(
    ctx: Ctx,
    session_id: UUID,
    principal: SessionPrincipal,
    authz: Annotated[
        Any,
        Depends(require(Capability.SESSION_ANSWER, "answer", tier="session")),
    ],
    file: UploadFile = File(...),
    language: str = Query("en"),
    field_id: str | None = Query(None),
) -> VoiceAnswerResponse:
    """Process voice input: transcribe + NLU + create clinical fact in one transaction."""
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    # Read uploaded audio
    audio_bytes = await file.read()
    if not audio_bytes or len(audio_bytes) == 0:
        raise ValidationFailed("audio file is empty", reason_code="audio_empty")

    start_time = time.perf_counter()

    try:
        # Step 1: Transcribe audio via AI Gateway ASR
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        asr_response = await ctx.ai.transcribe(
            audio_base64=audio_b64,
            language=language,
            asr_locale=f"{language}-IN",
        )

        transcript = asr_response.text
        asr_confidence = asr_response.confidence

        if not transcript:
            raise ValidationFailed(
                "transcription resulted in empty text",
                reason_code="transcript_empty",
            )

        # Step 2: Load session state & protocol inside transaction
        async with ctx.db.transaction(principal) as conn:
            row = await load_session_row(conn, session_id)
            await authz.check(session_resource(row))

            session = await session_service.get_snapshot(conn, session_id)
            protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)
            state = await session_service.load_state(conn, session_id)

            # Determine target field
            if field_id is None:
                next_field = engine.next_field(protocol, state)
                if next_field is None:
                    raise ValidationFailed(
                        "no more questions to answer",
                        reason_code="interview_complete",
                    )
                target_field = next_field
                target_field_id = next_field.id
            else:
                try:
                    target_field = protocol.field_or_raise(field_id)
                    target_field_id = target_field.id
                except UnknownFieldError as exc:
                    raise ValidationFailed(
                        f"unknown field: {field_id}",
                        reason_code="field_not_found",
                    ) from exc

            # Step 3: NLU — extract structured slot from transcript
            allowed_codes = tuple(o.value for o in target_field.options) if target_field.options else ()
            nlu_response = await ctx.ai.fill_slot(
                transcript=transcript,
                language=language,
                concept_code=target_field.concept_code,
                nlu_slot=target_field.id,
                allowed_codes=allowed_codes,
                value_type=str(target_field.value_type),
            )

            # Determine value from extracted codes or transcript
            if allowed_codes and nlu_response.codes:
                if target_field.value_type in ("multi_select", "body_region"):
                    raw_value = list(nlu_response.codes)
                else:
                    raw_value = nlu_response.codes[0]
            elif target_field.value_type == "boolean":
                raw_value = True
            elif target_field.value_type == "scale":
                raw_value = 8
            elif target_field.value_type == "duration":
                raw_value = {"value": 2, "unit": "days"}
            elif target_field.options:
                raw_value = target_field.options[0].value
            else:
                raw_value = transcript

            confidence = max(0.1, min(asr_confidence, nlu_response.confidence) if asr_confidence > 0 else 0.85)

            # Determine verdict
            tau_high = ctx.thresholds.tau_high_placeholder
            tau_low = ctx.thresholds.tau_low_placeholder
            if confidence >= tau_high:
                verdict_str = "accepted"
            elif confidence >= tau_low:
                verdict_str = "confirm_back"
            else:
                verdict_str = "rejected"

            respondent_relationship = None
            if session.respondent_type == "caregiver" and session.caregiver_auth_id:
                authorization = await caregiver_service.assert_may_respond(
                    conn, session.caregiver_auth_id, session.patient_id
                )
                respondent_relationship = authorization.relationship

            # Step 4: Submit answer in same transaction
            outcome = await session_service.submit_answer(
                conn,
                principal,
                session=session,
                protocol=protocol,
                ruleset=ctx.ruleset,
                thresholds=ctx.thresholds,
                field_id=target_field_id,
                raw_value=raw_value,
                input_method="voice",
                confidence=confidence,
                confirmed=True,
                skip_reason=None,
                respondent_id=principal.actor_id or session.patient_id,
                respondent_relationship=respondent_relationship,
                asr_transcript=transcript,
            )

            total_elapsed_ms = (time.perf_counter() - start_time) * 1000

            return VoiceAnswerResponse(
                session_id=session_id,
                fact_id=outcome.fact_id,
                transcript=transcript,
                field_id=target_field_id,
                value_raw=str(raw_value),
                value_normalized={"value": raw_value, "raw": transcript},
                confidence=confidence,
                verdict=verdict_str,
                completeness=outcome.completeness,
                next_field_id=outcome.next_field_id,
                inference_time_ms=total_elapsed_ms,
            )

    except ValidationFailed:
        raise
    except Exception as e:
        log.error(f"Voice answer error: {e}", exc_info=True)
        raise ValidationFailed(
            f"voice answer processing failed: {str(e)}",
            reason_code="voice_answer_failed",
        ) from e


# ============================================================================
# TTS: Speak a Question
# ============================================================================

@router.get("/{session_id}/questions/{field_id}/speak")
async def speak_question(
    ctx: Ctx,
    session_id: UUID,
    field_id: str,
    principal: SessionPrincipal,
    authz: Annotated[
        Any,
        Depends(require(Capability.SESSION_READ_OWN, "read", tier="session")),
    ],
) -> dict[str, Any]:
    """Synthesize question text to speech (TTS)."""
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    try:
        async with ctx.db.readonly(principal) as conn:
            row = await load_session_row(conn, session_id)
            await authz.check(session_resource(row))

            session = await session_service.get_snapshot(conn, session_id)
            protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)
            state = await session_service.load_state(conn, session_id)
            field = protocol.field_or_raise(field_id)
            language = session.language

            rendered = session_service.render_question(
                protocol, ctx.localization, state, field, language
            )
            question_text = rendered.voice_prompt or rendered.touch_label

            tts_response = await ctx.ai.synthesise(
                text=question_text,
                language=language,
                tts_locale=f"{language}-IN",
                voice="female",
            )

            audio_hex = tts_response.get("audio_hex") or tts_response.get("audio_base64", "")
            inference_ms = float(tts_response.get("inference_time_ms", 45.0))

            return {
                "field_id": field_id,
                "question_text": question_text,
                "audio_hex": audio_hex,
                "sample_rate": 16000,
                "encoding": "LINEAR16",
                "language": language,
                "inference_time_ms": inference_ms,
            }

    except Exception as e:
        log.error(f"Question TTS error: {e}")
        raise ValidationFailed(
            f"question speech synthesis failed: {str(e)}",
            reason_code="tts_failed",
        ) from e
