"""AI Gateway FastAPI application (CLAUDE.md §18, §20).

This service:
- Exposes ASR (speech-to-text) endpoints
- Exposes TTS (text-to-speech) endpoints
- Has NO database access (network + code enforcement)
- Never writes to clinical database
- Uses Bhashini/AI4Bharat APIs

[RED LINE §20] Network: ai-net bridge only, PostgreSQL not accessible.
[RED LINE §20] Code: No DB client, no ORM, no connection string.
[RED LINE §20] CI: Build fails if credentials appear in config.
"""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field as PField
from pydantic_settings import BaseSettings

from medikiosk_ai.asr import ASRConfig, ASRGateway, ASRRequest, ASRResponse, VADConfig
from medikiosk_ai.tts import TTSConfig, TTSGateway, TTSStreamer

# Logging setup (no PHI/PII, §28)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class AIGatewaySettings(BaseSettings):
    """Configuration from environment (§32 — no .env file)."""
    
    environment: str = "local"
    service_name: str = "medikiosk-ai-gateway"
    api_root_path: str = ""
    
    # Bhashini API credentials (from secrets store in production)
    bhashini_api_key: str = "sandbox-key-local-dev-only"
    bhashini_asr_endpoint: str = "https://api.bhashini.gov.in/asr/v1"
    bhashini_tts_endpoint: str = "https://api.bhashini.gov.in/tts/v1"
    
    # ASR/TTS configuration
    asr_model_id: str = "model_asr_hi_en"
    tts_model_id: str = "model_tts_hi_en"
    audio_sample_rate: int = 16000
    audio_encoding: str = "LINEAR16"
    
    # Timeouts (§54)
    asr_timeout_seconds: float = 5.0
    tts_timeout_seconds: float = 3.0
    
    class Config:
        env_prefix = "MEDIKIOSK_AI_"
        env_file = None  # [RED LINE §32] no .env loading


# Global gateway instances
asr_gateway: ASRGateway | None = None
tts_gateway: TTSGateway | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize gateways on startup, cleanup on shutdown."""
    global asr_gateway, tts_gateway
    
    settings = AIGatewaySettings()
    
    asr_gateway = ASRGateway(
        ASRConfig(
            api_endpoint=settings.bhashini_asr_endpoint,
            api_key=settings.bhashini_api_key,
            model_id=settings.asr_model_id,
            sample_rate=settings.audio_sample_rate,
            encoding=settings.audio_encoding,
        ),
        vad_config=VADConfig(),
    )
    
    tts_gateway = TTSGateway(
        TTSConfig(
            api_endpoint=settings.bhashini_tts_endpoint,
            api_key=settings.bhashini_api_key,
            model_id=settings.tts_model_id,
            sample_rate=settings.audio_sample_rate,
            encoding=settings.audio_encoding,
        )
    )
    
    logger.info("AI Gateway started: ASR + TTS ready")
    
    yield
    
    # Cleanup
    if asr_gateway:
        await asr_gateway.close()
    if tts_gateway:
        await tts_gateway.close()
    
    logger.info("AI Gateway shutdown")


app = FastAPI(
    title="MediKiosk AI Gateway",
    description="Isolated ASR/TTS service (§18, §20)",
    version="0.1.0",
    root_path=AIGatewaySettings().api_root_path,
    lifespan=lifespan,
)


# ============================================================================
# ASR Endpoints (Speech-to-Text)
# ============================================================================

class ASRStreamRequest(BaseModel):
    """Streaming ASR request (WebSocket or chunked upload)."""
    
    language: str = PField(default="hi")
    audio_format: str = PField(default="LINEAR16")
    sample_rate: int = PField(default=16000)


class NLUSlotFillRequest(BaseModel):
    """NLU slot-filling: extract clinical concept from transcript."""
    
    transcript: str = PField(..., min_length=1)
    field_id: str = PField(...)  # Protocol field to extract into
    language: str = PField(default="hi")


class NLUSlotFillResponse(BaseModel):
    """Extracted clinical value."""
    
    field_id: str
    value_raw: str
    value_normalized: dict
    confidence: float = PField(..., ge=0.0, le=1.0)
    inference_time_ms: float


@app.get("/healthz")
async def health():
    """Health check."""
    return {"status": "ok", "service": "medikiosk-ai-gateway"}


@app.post("/v1/asr/transcribe", response_model=ASRResponse)
async def transcribe(
    language: str = Query("hi"),
    file: UploadFile = File(...),
) -> ASRResponse:
    """Transcribe uploaded audio file (non-streaming).
    
    §18.2: Simpler interface for testing. Streaming is via WebSocket or chunked POST.
    
    Args:
        language: Language code (hi, en, ta, te, ml)
        file: Audio file (PCM WAV recommended)
    
    Returns:
        ASRResponse with transcript and confidence
    """
    if asr_gateway is None:
        raise HTTPException(status_code=503, detail="ASR not initialized")
    
    try:
        audio_bytes = await file.read()
        response = await asr_gateway.transcribe_full(audio_bytes, language=language)
        return response
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/asr/stream")
async def asr_stream_endpoint(
    language: str = Query("hi"),
    file: UploadFile = File(...),
) -> dict:
    """Streaming ASR (mock implementation for SIH).
    
    In production, this would be a WebSocket. For Phase 3 SIH, we mock it
    as a chunked endpoint that simulates streaming by processing the full
    audio and returning intermediate results.
    
    §18.2: Partial hypotheses emitted continuously.
    
    Args:
        language: Language code
        file: Audio file
    
    Returns:
        Final transcription result
    """
    if asr_gateway is None:
        raise HTTPException(status_code=503, detail="ASR not initialized")
    
    try:
        audio_bytes = await file.read()
        # Simulate streaming by calling transcribe_full
        response = await asr_gateway.transcribe_full(audio_bytes, language=language)
        return {
            "transcript": response.transcript,
            "confidence": response.confidence,
            "is_final": response.is_final,
            "language": language,
            "inference_time_ms": response.inference_time_ms,
        }
    except Exception as e:
        logger.error(f"Stream ASR error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/nlu/slot-fill", response_model=NLUSlotFillResponse)
async def nlu_slot_fill(payload: NLUSlotFillRequest) -> NLUSlotFillResponse:
    """Slot-fill transcript into a clinical field.
    
    CLAUDE.md §10: NLU is not ML-ranked; it validates the transcript against
    the expected field type and normalizes the value. For example, "chest pain
    for two days" → {concept: "symptom.duration", value: 2, unit: "days"}.
    
    This is a mock implementation. Real implementation would use a rule-based
    or small language model (not the large LLM).
    
    Args:
        payload: Transcript + field_id to extract into
    
    Returns:
        Normalized clinical value with confidence
    """
    logger.info(
        f"NLU slot-fill: field_id={payload.field_id}, "
        f"transcript_len={len(payload.transcript)}"
    )
    
    # Mock implementation: return a high-confidence normalized value
    # Real implementation would parse the transcript semantically
    import time
    start = time.time()
    
    # Simulate NLU processing
    normalized = {
        "raw": payload.transcript,
        "field_id": payload.field_id,
        "confidence": 0.85,  # Mock confidence
    }
    
    inference_ms = (time.time() - start) * 1000
    
    return NLUSlotFillResponse(
        field_id=payload.field_id,
        value_raw=payload.transcript,
        value_normalized=normalized,
        confidence=0.85,
        inference_time_ms=inference_ms,
    )


# ============================================================================
# TTS Endpoints (Text-to-Speech)
# ============================================================================

class TTSRequest(BaseModel):
    """TTS request schema."""
    
    text: str = PField(..., min_length=1, max_length=1000)
    language: str = PField(default="hi")
    voice_gender: str = PField(default="female")


@app.post("/v1/tts/synthesize")
async def synthesize_speech(payload: TTSRequest):
    """Synthesize speech from text.
    
    CLAUDE.md §54: TTS is streamed and non-blocking. This endpoint returns
    audio bytes; the caller streams them asynchronously to the patient.
    
    Args:
        payload: Text + language
    
    Returns:
        Audio in LINEAR16 PCM format (or streaming response)
    """
    if tts_gateway is None:
        raise HTTPException(status_code=503, detail="TTS not initialized")
    
    try:
        response = await tts_gateway.synthesize(
            payload.text,
            language=payload.language,
            voice_gender=payload.voice_gender,
        )
        return {
            "audio_base64": response.audio_bytes.hex(),  # Hex-encoded
            "language": response.language,
            "sample_rate": 16000,
            "encoding": "LINEAR16",
            "inference_time_ms": response.inference_time_ms,
        }
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/tts/question")
async def speak_question(
    question_text: str = Query(...),
    language: str = Query("hi"),
):
    """Speak a protocol question (convenience endpoint).
    
    Combines protocol question rendering + TTS in one call.
    
    Args:
        question_text: Question to speak
        language: Language code
    
    Returns:
        Audio bytes
    """
    if tts_gateway is None:
        raise HTTPException(status_code=503, detail="TTS not initialized")
    
    try:
        response = await tts_gateway.synthesize(question_text, language=language)
        return {
            "audio_hex": response.audio_bytes.hex(),
            "language": language,
            "duration_approx_seconds": len(response.audio_bytes) / (16000 * 2),  # ~estimate
        }
    except Exception as e:
        logger.error(f"Question TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Diagnostic / Monitoring
# ============================================================================

@app.get("/v1/meta/models")
async def list_models():
    """List available ASR/TTS models."""
    settings = AIGatewaySettings()
    return {
        "asr_model": settings.asr_model_id,
        "tts_model": settings.tts_model_id,
        "supported_languages": ["hi", "en", "ta", "te", "ml"],
        "audio_config": {
            "sample_rate": settings.audio_sample_rate,
            "encoding": settings.audio_encoding,
        },
    }


@app.get("/readyz")
async def readiness():
    """Readiness check: all gateways initialized."""
    ready = asr_gateway is not None and tts_gateway is not None
    if not ready:
        raise HTTPException(status_code=503, detail="Gateways not initialized")
    return {"ready": True, "service": "medikiosk-ai-gateway"}


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8100,
        log_level="info",
    )
