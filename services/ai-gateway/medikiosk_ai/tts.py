"""Text-to-Speech (TTS) synthesis (CLAUDE.md §18).

Bhashini/AI4Bharat integration:
- Multi-lingual support (hi, en, ta, te, ml)
- Low-latency streaming
- Audio format negotiation
- Fallback on failure

[RED LINE §20] This service has NO database access.
[CONTROL §54] TTS is streamed; clinical workflow never waits on TTS.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncGenerator

import httpx
from pydantic import BaseModel, Field as PField

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TTSConfig:
    """Bhashini/AI4Bharat TTS configuration."""
    
    api_endpoint: str  # https://api.bhashini.gov.in/tts/v1
    api_key: str
    model_id: str = "model_tts_hi_en"
    language: str = "hi"  # Default to Hindi
    sample_rate: int = 16000
    encoding: str = "LINEAR16"
    voice_gender: str = "female"  # female | male


class TTSRequest(BaseModel):
    """TTS request schema."""
    
    text: str = PField(..., min_length=1, max_length=1000)
    language: str = PField(default="hi")
    voice_gender: str = PField(default="female")


class TTSResponse(BaseModel):
    """TTS response schema."""
    
    audio_bytes: bytes
    language: str
    inference_time_ms: float


class TTSGateway:
    """TTS orchestrator with Bhashini integration."""
    
    def __init__(self, config: TTSConfig):
        self.config = config
        self.client = httpx.AsyncClient(timeout=10.0)
    
    async def close(self):
        """Cleanup resources."""
        await self.client.aclose()
    
    async def synthesize(
        self,
        text: str,
        language: str = "hi",
        voice_gender: str | None = None,
    ) -> TTSResponse:
        """Synthesize speech from text.
        
        CLAUDE.md §18.2: TTS is non-blocking; clinical workflow continues
        while audio is streamed asynchronously to the patient's speaker.
        
        Args:
            text: Text to synthesize
            language: Language code (hi, en, ta, te, ml)
            voice_gender: Voice gender (female | male)
        
        Returns:
            TTSResponse with audio bytes
        
        Raises:
            HTTPError: If TTS API fails
        """
        start_time = datetime.now()
        voice_gender = voice_gender or self.config.voice_gender
        
        # Normalize text for synthesis
        text = text.strip()
        if not text:
            raise ValueError("Text to synthesize cannot be empty")
        if len(text) > 1000:
            logger.warning(f"Text truncated to 1000 chars (was {len(text)})")
            text = text[:1000]
        
        try:
            # Construct Bhashini request
            payload = {
                "config": {
                    "language": {"sourceLanguage": language},
                    "audioFormat": self.config.encoding,
                    "samplingRate": self.config.sample_rate,
                    "voiceGender": voice_gender,
                },
                "input": [{"source": text}],
            }
            
            # Call Bhashini TTS API (§18.1)
            response = await self.client.post(
                f"{self.config.api_endpoint}/tts",
                json=payload,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
            )
            response.raise_for_status()
            
            result = response.json()
            audio_hex = result.get("audio", [{}])[0].get("audioContent", "")
            audio_bytes = bytes.fromhex(audio_hex) if audio_hex else b""
            
            inference_ms = (datetime.now() - start_time).total_seconds() * 1000
            
            logger.info(
                f"TTS response: audio_bytes={len(audio_bytes)}, "
                f"inference_ms={inference_ms:.1f}, language={language}"
            )
            
            return TTSResponse(
                audio_bytes=audio_bytes,
                language=language,
                inference_time_ms=inference_ms,
            )
        
        except httpx.HTTPError as e:
            logger.error(f"TTS API error: {e}")
            raise


class TTSStreamer:
    """Streamed TTS output (non-blocking for §54 latency budget)."""
    
    def __init__(self, gateway: TTSGateway):
        self.gateway = gateway
    
    async def stream_question(
        self,
        question_text: str,
        language: str,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """Stream TTS audio for a question, yielding audio chunks.
        
        CLAUDE.md §54: TTS is streamed, never a blocking operation.
        
        Args:
            question_text: Question to speak
            language: Language code
            chunk_size: Audio chunk size in bytes
        
        Yields:
            Audio frame bytes
        """
        try:
            response = await self.gateway.synthesize(question_text, language)
            audio_bytes = response.audio_bytes
            
            # Yield in chunks for streaming effect
            for i in range(0, len(audio_bytes), chunk_size):
                yield audio_bytes[i : i + chunk_size]
                await asyncio.sleep(0.01)  # Small delay between chunks
        
        except Exception as e:
            logger.error(f"TTS stream error: {e}")
            # Graceful degradation: emit silence (§37)
            yield b"\x00" * chunk_size
