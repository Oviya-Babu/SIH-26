"""Real Automatic Speech Recognition using faster-whisper (CTranslate2).

This replaces the mock Bhashini ASR with an actual local model:
- faster-whisper-small with INT8 quantization for CPU
- Supports English, Hindi, Tamil, Telugu, Malayalam
- No external API calls, fully offline after model download

[RED LINE §20] This service has NO database access.
[CONTROL §54] Latency budget: ASR final <800ms p95.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Language code mapping: our codes → Whisper language codes
LANGUAGE_MAP = {
    "en": "en",
    "hi": "hi",
    "ta": "ta",
    "te": "te",
    "ml": "ml",
}

# Supported languages (what we actually claim to support)
SUPPORTED_LANGUAGES = frozenset(LANGUAGE_MAP.keys())


@dataclass(frozen=True, slots=True)
class ASRConfig:
    """Local faster-whisper ASR configuration."""
    model_path: str = ""  # Path to faster-whisper model directory
    model_size: str = "small"  # Model size identifier
    compute_type: str = "int8"  # INT8 for CPU efficiency
    cpu_threads: int = 4  # Number of CPU threads
    beam_size: int = 3  # Beam search size (lower = faster)
    best_of: int = 1  # Number of candidates
    sample_rate: int = 16000
    vad_filter: bool = True  # Use built-in VAD filter


@dataclass(frozen=True, slots=True)
class ASRResult:
    """ASR transcription result."""
    transcript: str
    confidence: float
    language: str
    is_final: bool
    inference_time_ms: float
    model_version: str
    audio_duration_seconds: float


class LocalASREngine:
    """Real ASR using faster-whisper with CTranslate2 backend.

    This loads an actual speech recognition model and transcribes
    real audio from the microphone. No hardcoded transcripts.
    """

    def __init__(self, config: ASRConfig) -> None:
        self.config = config
        self._model = None
        self._loaded = False
        self._load_time_ms: float = 0

    def _ensure_loaded(self) -> None:
        """Lazy-load the model on first use."""
        if self._loaded:
            return

        from faster_whisper import WhisperModel

        model_path = self.config.model_path
        if not model_path or not Path(model_path).exists():
            # Try downloading from HF if path doesn't exist
            model_path = self.config.model_size
            logger.info(f"Model path not found, using model size: {model_path}")

        start = time.perf_counter()

        self._model = WhisperModel(
            model_path,
            device="cpu",
            compute_type=self.config.compute_type,
            cpu_threads=self.config.cpu_threads,
            num_workers=1,
        )

        # Warm-up with a silent buffer so subsequent calls are instant
        try:
            dummy = np.zeros(1600, dtype=np.float32)
            list(self._model.transcribe(dummy, language="en", beam_size=1, condition_on_previous_text=False)[0])
        except Exception as exc:
            logger.debug(f"ASR warm-up ignored: {exc}")

        self._load_time_ms = (time.perf_counter() - start) * 1000
        self._loaded = True
        logger.info(
            f"ASR model loaded and warmed up: model={model_path}, "
            f"compute_type={self.config.compute_type}, "
            f"load_time_ms={self._load_time_ms:.1f}, "
            f"cpu_threads={self.config.cpu_threads}"
        )


    def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "en",
        *,
        is_final: bool = True,
    ) -> ASRResult:
        """Transcribe audio bytes into text.

        Args:
            audio_bytes: Raw audio bytes (WAV or raw PCM 16-bit)
            language: Language code (en, hi, ta, te, ml)
            is_final: Whether this is the final transcription

        Returns:
            ASRResult with real transcript and confidence
        """
        self._ensure_loaded()

        start = time.perf_counter()

        # Convert audio bytes to float32 numpy array
        audio_array = self._decode_audio(audio_bytes)
        audio_duration = len(audio_array) / self.config.sample_rate

        if len(audio_array) == 0 or audio_duration < 0.1:
            return ASRResult(
                transcript="",
                confidence=0.0,
                language=language,
                is_final=is_final,
                inference_time_ms=0.0,
                model_version=f"faster-whisper-{self.config.model_size}",
                audio_duration_seconds=audio_duration,
            )

        # Map language code
        whisper_lang = LANGUAGE_MAP.get(language, "en")

        try:
            segments, info = self._model.transcribe(
                audio_array,
                language=whisper_lang,
                beam_size=self.config.beam_size,
                best_of=self.config.best_of,
                vad_filter=self.config.vad_filter,
                vad_parameters=dict(
                    min_speech_duration_ms=250,
                    min_silence_duration_ms=300,
                    speech_pad_ms=30,
                ),
                word_timestamps=False,
                condition_on_previous_text=False,
            )

            # Collect all segment texts
            texts = []
            total_log_prob = 0.0
            segment_count = 0

            for segment in segments:
                text = segment.text.strip()
                if text:
                    texts.append(text)
                    total_log_prob += getattr(segment, "avg_logprob", getattr(segment, "avg_log_prob", -0.2))
                    segment_count += 1


            transcript = " ".join(texts).strip()

            # Compute confidence from average log probability
            # Whisper log probs are typically in [-1, 0], map to [0, 1]
            if segment_count > 0:
                avg_log_prob = total_log_prob / segment_count
                # Map from log space: exp(avg_log_prob) gives [0, 1]
                confidence = min(1.0, max(0.0, np.exp(avg_log_prob)))
            else:
                confidence = 0.0

            inference_ms = (time.perf_counter() - start) * 1000

            logger.info(
                f"ASR transcribed: lang={whisper_lang}, "
                f"transcript_len={len(transcript)}, "
                f"confidence={confidence:.3f}, "
                f"inference_ms={inference_ms:.1f}, "
                f"audio_duration_s={audio_duration:.1f}, "
                f"detected_lang={info.language}, "
                f"lang_prob={info.language_probability:.2f}"
            )

            return ASRResult(
                transcript=transcript,
                confidence=round(confidence, 3),
                language=language,
                is_final=is_final,
                inference_time_ms=round(inference_ms, 1),
                model_version=f"faster-whisper-{self.config.model_size}",
                audio_duration_seconds=round(audio_duration, 2),
            )

        except Exception as e:
            inference_ms = (time.perf_counter() - start) * 1000
            logger.error(f"ASR transcription error: {e}", exc_info=True)
            return ASRResult(
                transcript="",
                confidence=0.0,
                language=language,
                is_final=is_final,
                inference_time_ms=round(inference_ms, 1),
                model_version=f"faster-whisper-{self.config.model_size}",
                audio_duration_seconds=round(audio_duration, 2),
            )

    def _decode_audio(self, audio_bytes: bytes) -> np.ndarray:
        """Decode audio bytes to float32 numpy array.

        Handles WAV files and raw PCM 16-bit audio.
        """
        if not audio_bytes:
            return np.array([], dtype=np.float32)

        # Try WAV format first
        if audio_bytes[:4] == b"RIFF":
            try:
                with io.BytesIO(audio_bytes) as buf:
                    with wave.open(buf, "rb") as wf:
                        n_channels = wf.getnchannels()
                        sample_width = wf.getsampwidth()
                        framerate = wf.getframerate()
                        n_frames = wf.getnframes()
                        raw = wf.readframes(n_frames)

                        # Convert to int16
                        if sample_width == 2:
                            samples = np.frombuffer(raw, dtype=np.int16)
                        elif sample_width == 1:
                            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
                        else:
                            # Try as int16 anyway
                            samples = np.frombuffer(raw, dtype=np.int16)

                        # Mono mixdown
                        if n_channels > 1:
                            samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

                        # Convert to float32 [-1, 1]
                        audio = samples.astype(np.float32) / 32768.0

                        # Resample if needed
                        if framerate != self.config.sample_rate:
                            audio = self._resample(audio, framerate, self.config.sample_rate)

                        return audio
            except Exception as e:
                logger.warning(f"WAV decode failed, trying raw PCM: {e}")

        # Assume raw PCM 16-bit mono
        try:
            samples = np.frombuffer(audio_bytes, dtype=np.int16)
            return samples.astype(np.float32) / 32768.0
        except Exception as e:
            logger.error(f"Audio decode failed: {e}")
            return np.array([], dtype=np.float32)

    def _resample(
        self, audio: np.ndarray, orig_sr: int, target_sr: int
    ) -> np.ndarray:
        """Simple linear interpolation resampling."""
        if orig_sr == target_sr:
            return audio
        ratio = target_sr / orig_sr
        new_length = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_length)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded,
            "model_path": self.config.model_path,
            "model_size": self.config.model_size,
            "compute_type": self.config.compute_type,
            "load_time_ms": self._load_time_ms,
            "supported_languages": sorted(SUPPORTED_LANGUAGES),
            "cpu_threads": self.config.cpu_threads,
        }
