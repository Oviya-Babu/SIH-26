"""Voice answer endpoints for Phase 3 (CLAUDE.md §3, §18, §54).

These endpoints wire the AI Gateway ASR/NLU into the interactive session loop:

    POST /v1/sessions/{id}/answers/voice → ASR transcription
    POST /v1/sessions/{id}/answers/voice → NLU + slot-fill + answer

All synchronous, same-transaction with clinical fact persistence (§50).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, SessionPrincipal, load_session_row, require, session_resource
from medikiosk.errors import Forbidden, ValidationFailed
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
    # Audio file is passed via multipart/form-data in the HTTP request


class VoiceTranscriptionResponse(BaseModel):
    """Transcription result from ASR."""
    
    transcript: str
    confidence: float = PField(..., ge=0.0, le=1.0)
    language: str
    inference_time_ms: float
    # Caller decides whether confidence is sufficient; if not, re-prompt


class VoiceAnswerResponse(BaseModel):
    """Voice answer processed → clinical fact created."""
    
    session_id: UUID
    fact_id: UUID
    transcript: str
    field_id: str
    value_raw: str
    value_normalized: dict[str, Any]
    confidence: float
    verdict: str  # accepted | too_low_confidence | ...
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
    """Transcribe voice to text (ASR only, no answer processing yet).
    
    CLAUDE.md §18.2: ASR is the first step. The kiosk can then:
    1. Present the transcript to the patient for confirmation
    2. Silently accept if confidence is high enough
    3. Re-prompt if confidence is low
    
    The confidence verdict is determined by τ_high/τ_low thresholds (§54).
    
    Args:
        session_id: Patient session
        file: Audio file (PCM recommended)
        language: Language code (en, hi, ta, te, ml)
    
    Returns:
        VoiceTranscriptionResponse with transcript + confidence
    
    Raises:
        ValidationFailed: If audio is corrupted or ASR fails
    """
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

        # Read audio from upload
        audio_bytes = await file.read()

        # Call AI Gateway ASR endpoint
        asr_response = await ctx.ai_gateway.transcribe(
            language=language,
            audio_bytes=audio_bytes,
        )
        
        log.info(
            f"Voice transcription: session_id={session_id}, "
            f"transcript_len={len(asr_response.transcript)}, "
            f"confidence={asr_response.confidence:.2f}"
        )
        
        return VoiceTranscriptionResponse(
            transcript=asr_response.transcript,
            confidence=asr_response.confidence,
            language=language,
            inference_time_ms=asr_response.inference_time_ms,
        )
    
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        raise ValidationFailed(
            f"transcription error: {str(e)}",
            reason_code="asr_failed",
        )


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
    """Process voice input: transcribe + NLU + create clinical fact (same transaction).
    
    CLAUDE.md §18.2: Complete voice answer flow:
    1. ASR: audio → transcript
    2. NLU: transcript → normalized clinical value
    3. Validate: confidence gates (§10, τ_high/τ_low)
    4. Persist: clinical fact + audit in same transaction
    5. Recompute: red flags, completeness, next field
    
    CLAUDE.md §54 latency budget: entire flow <1.5s p95
    
    Args:
        session_id: Patient session
        file: Audio file (PCM)
        language: Language code
        field_id: Override which field to answer (defaults to next_field)
    
    Returns:
        VoiceAnswerResponse with fact_id, verdict, completeness
    
    Raises:
        ValidationFailed: If transcription or NLU fails
        Forbidden: If session is sealed
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    if file.size is None or file.size == 0:
        raise ValidationFailed("audio file is empty", reason_code="audio_empty")

    try:
        async with ctx.db.readonly(principal) as conn:
            row = await load_session_row(conn, session_id)
            await authz.check(session_resource(row))

        # Step 1: Transcribe audio
        audio_bytes = await file.read()
        asr_response = await ctx.ai_gateway.transcribe(
            language=language,
            audio_bytes=audio_bytes,
        )
        
        transcript = asr_response.transcript
        asr_confidence = asr_response.confidence
        
        if not transcript:
            raise ValidationFailed(
                "transcription resulted in empty text",
                reason_code="transcript_empty",
            )
        
        # Step 2: Get current session state to determine field
        async with ctx.db.transaction(principal) as conn:
            session_row = await session_service.load_session(
                conn, session_id, principal
            )
            
            protocol = ctx.protocols.get(
                session_row["protocol_family"],
                session_row["protocol_version"],
            )
            
            if protocol is None:
                raise ValidationFailed(
                    "protocol not found",
                    reason_code="protocol_not_found",
                )
            
            # Determine target field
            if field_id is None:
                # Use next_field from protocol
                state = await session_service.load_state(conn, session_id, principal)
                next_field = engine.next_field(protocol, state)
                if next_field is None:
                    raise ValidationFailed(
                        "no more questions to answer",
                        reason_code="interview_complete",
                    )
                field_id = next_field.id
            else:
                # Validate field_id exists in protocol
                try:
                    protocol.get_field(field_id)
                except UnknownFieldError:
                    raise ValidationFailed(
                        f"unknown field: {field_id}",
                        reason_code="field_not_found",
                    )
            
            # Step 3: NLU — slot-fill transcript into field
            nlu_response = await ctx.ai_gateway.slot_fill(
                transcript=transcript,
                field_id=field_id,
                language=language,
            )
            
            value_raw = nlu_response.value_raw
            value_normalized = nlu_response.value_normalized
            confidence = nlu_response.confidence
            
            # Step 4: Validate confidence against field thresholds
            tau_high = protocol.get_field(field_id).confidence_threshold_high or 0.75
            tau_low = protocol.get_field(field_id).confidence_threshold_low or 0.4
            
            if confidence >= tau_high:
                verdict = ConfidenceVerdict.ACCEPTED
            elif tau_low <= confidence < tau_high:
                verdict = ConfidenceVerdict.CONFIRM_BACK
            else:
                verdict = ConfidenceVerdict.REJECTED
            
            # Step 5: Submit answer (same transaction with audit, red flags, etc.)
            # Re-use the typed answer flow; the fact will be marked as source_type=voice_answer
            answer_outcome = await session_service.submit_answer(
                conn,
                session_id,
                principal,
                field_id=field_id,
                value_raw=value_raw,
                value_normalized=value_normalized,
                confidence=confidence,
                source_type="voice_answer",
                language=language,
            )
            
            # Step 6: Compute total inference time
            total_inference_ms = (
                asr_response.inference_time_ms
                + nlu_response.inference_time_ms
            )
            
            log.info(
                f"Voice answer: session_id={session_id}, field_id={field_id}, "
                f"transcript_len={len(transcript)}, verdict={verdict}, "
                f"completeness={answer_outcome.completeness:.2f}, "
                f"total_inference_ms={total_inference_ms:.1f}"
            )
            
            return VoiceAnswerResponse(
                session_id=session_id,
                fact_id=answer_outcome.fact_id,
                transcript=transcript,
                field_id=field_id,
                value_raw=value_raw,
                value_normalized=value_normalized,
                confidence=confidence,
                verdict=verdict.value,
                completeness=answer_outcome.completeness,
                next_field_id=answer_outcome.next_field_id,
                inference_time_ms=total_inference_ms,
            )
    
    except ValidationFailed:
        raise
    except Exception as e:
        log.error(f"Voice answer error: {e}", exc_info=True)
        raise ValidationFailed(
            f"voice answer processing failed: {str(e)}",
            reason_code="voice_answer_failed",
        )


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
    """Synthesize question text to speech (TTS).
    
    CLAUDE.md §18, §54: TTS is streamed and non-blocking. The kiosk plays
    audio asynchronously while remaining responsive to touch input.
    
    Args:
        session_id: Patient session
        field_id: Field whose question to speak
    
    Returns:
        Audio metadata + content
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    try:
        async with ctx.db.transaction(principal) as conn:
            row = await load_session_row(conn, session_id)
            await authz.check(session_resource(row))
            session_row = await session_service.load_session(
                conn, session_id, principal
            )
            
            protocol = ctx.protocols.get(
                session_row["protocol_family"],
                session_row["protocol_version"],
            )
            
            field = protocol.get_field(field_id)
            language = session_row["language"]
            
            # Render question text (localized)
            question_text = ctx.localization.render_question(
                protocol.family,
                field.id,
                language,
            )
            
            # Call TTS
            tts_response = await ctx.ai_gateway.synthesize(
                text=question_text,
                language=language,
            )
            
            log.info(
                f"Question TTS: field_id={field_id}, "
                f"audio_bytes={len(tts_response.audio_bytes)}, "
                f"language={language}"
            )
            
            return {
                "field_id": field_id,
                "question_text": question_text,
                "audio_hex": tts_response.audio_bytes.hex(),
                "sample_rate": 16000,
                "encoding": "LINEAR16",
                "language": language,
                "inference_time_ms": tts_response.inference_time_ms,
            }
    
    except Exception as e:
        log.error(f"Question TTS error: {e}")
        raise ValidationFailed(
            f"question speech synthesis failed: {str(e)}",
            reason_code="tts_failed",
        )



