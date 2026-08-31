"""Voice Activity Detection using Silero VAD v5 (ONNX Runtime CPU).

Real neural VAD — not energy-based heuristics. Silero VAD is a small (~2MB)
ONNX model that outputs speech probability per audio frame.

[RED LINE §20] This service has NO database access.
[CONTROL] intra_op_num_threads=2 to limit CPU contention.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Lazy-loaded ONNX Runtime to avoid import-time overhead
_ort = None


def _get_ort():
    global _ort
    if _ort is None:
        import onnxruntime as ort
        _ort = ort
    return _ort


@dataclass(frozen=True, slots=True)
class VADConfig:
    """Silero VAD configuration."""
    model_path: str = ""  # Path to silero_vad.onnx
    sample_rate: int = 16000
    # Speech probability threshold (0.0–1.0)
    speech_threshold: float = 0.5
    # Minimum speech duration to start recording (ms)
    min_speech_duration_ms: int = 250
    # Minimum silence duration to end speech (ms)
    min_silence_duration_ms: int = 300
    # Frame size in samples (512 for 16kHz = 32ms)
    frame_size: int = 512
    # ONNX Runtime threads
    intra_op_num_threads: int = 2


@dataclass
class VADSegment:
    """A detected speech segment."""
    start_sample: int
    end_sample: int
    start_seconds: float
    end_seconds: float
    speech_probability: float


class SileroVADEngine:
    """Real Silero VAD v5 using ONNX Runtime.

    The model outputs a speech probability for each audio frame.
    We use a state machine to detect speech start and end.
    """

    def __init__(self, config: VADConfig) -> None:
        self.config = config
        self._session = None
        self._h = None  # Hidden state
        self._c = None  # Cell state
        self._loaded = False
        self._load_time_ms: float = 0

    def _ensure_loaded(self) -> None:
        """Lazy-load the ONNX model on first use."""
        if self._loaded:
            return

        ort = _get_ort()
        model_path = self.config.model_path

        if not model_path or not Path(model_path).is_file():
            raise FileNotFoundError(
                f"Silero VAD model not found at: {model_path}. "
                f"Run scripts/download_models.py to download it."
            )

        start = time.perf_counter()

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = self.config.intra_op_num_threads
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            model_path,
            sess_options,
            providers=["CPUExecutionProvider"],
        )

        # Initialize hidden states (Silero VAD v5 uses 2 layers, 64 units)
        self._reset_states()

        self._load_time_ms = (time.perf_counter() - start) * 1000
        self._loaded = True
        logger.info(
            f"Silero VAD loaded: model={model_path}, "
            f"load_time_ms={self._load_time_ms:.1f}, "
            f"threads={self.config.intra_op_num_threads}"
        )

    def _reset_states(self) -> None:
        """Reset LSTM hidden states for a new audio stream."""
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def speech_probability(self, audio_frame: np.ndarray) -> float:
        """Get speech probability for a single audio frame.

        Args:
            audio_frame: Float32 array of shape (frame_size,), values in [-1, 1]

        Returns:
            Speech probability in [0, 1]
        """
        self._ensure_loaded()

        if len(audio_frame) != self.config.frame_size:
            # Pad or truncate to expected frame size
            if len(audio_frame) < self.config.frame_size:
                audio_frame = np.pad(
                    audio_frame,
                    (0, self.config.frame_size - len(audio_frame)),
                )
            else:
                audio_frame = audio_frame[:self.config.frame_size]

        # Prepare inputs
        input_data = audio_frame.reshape(1, -1).astype(np.float32)
        sr = np.array([self.config.sample_rate], dtype=np.int64)

        try:
            outputs = self._session.run(
                None,
                {
                    "input": input_data,
                    "sr": sr,
                    "h": self._h,
                    "c": self._c,
                },
            )
            prob = float(outputs[0].item())
            self._h = outputs[1]
            self._c = outputs[2]
            return prob

        except Exception as e:
            logger.error(f"VAD inference error: {e}")
            return 0.0

    def detect_speech_segments(
        self, audio: np.ndarray, sample_rate: int = 16000
    ) -> list[VADSegment]:
        """Detect speech segments in a complete audio buffer.

        Args:
            audio: Float32 array of audio samples in [-1, 1]
            sample_rate: Sample rate (must be 16000 or 8000)

        Returns:
            List of detected speech segments
        """
        self._ensure_loaded()
        self._reset_states()

        if sample_rate != self.config.sample_rate:
            raise ValueError(
                f"Expected sample rate {self.config.sample_rate}, got {sample_rate}"
            )

        frame_size = self.config.frame_size
        total_samples = len(audio)
        segments: list[VADSegment] = []

        speech_start: int | None = None
        silence_start: int | None = None
        min_speech_samples = int(
            self.config.min_speech_duration_ms * sample_rate / 1000
        )
        min_silence_samples = int(
            self.config.min_silence_duration_ms * sample_rate / 1000
        )

        probs: list[float] = []

        for i in range(0, total_samples, frame_size):
            frame = audio[i : i + frame_size]
            if len(frame) < frame_size:
                frame = np.pad(frame, (0, frame_size - len(frame)))

            prob = self.speech_probability(frame)
            probs.append(prob)

            is_speech = prob >= self.config.speech_threshold

            if is_speech:
                silence_start = None
                if speech_start is None:
                    speech_start = i
            else:
                if speech_start is not None:
                    if silence_start is None:
                        silence_start = i
                    elif (i - silence_start) >= min_silence_samples:
                        # Speech ended
                        duration = silence_start - speech_start
                        if duration >= min_speech_samples:
                            avg_prob = np.mean(
                                probs[
                                    speech_start // frame_size : silence_start // frame_size
                                ]
                            ) if probs else 0.0
                            segments.append(
                                VADSegment(
                                    start_sample=speech_start,
                                    end_sample=silence_start,
                                    start_seconds=speech_start / sample_rate,
                                    end_seconds=silence_start / sample_rate,
                                    speech_probability=float(avg_prob),
                                )
                            )
                        speech_start = None
                        silence_start = None

        # Handle trailing speech
        if speech_start is not None:
            end = silence_start if silence_start is not None else total_samples
            duration = end - speech_start
            if duration >= min_speech_samples:
                avg_prob = np.mean(
                    probs[speech_start // frame_size :]
                ) if probs else 0.0
                segments.append(
                    VADSegment(
                        start_sample=speech_start,
                        end_sample=end,
                        start_seconds=speech_start / sample_rate,
                        end_seconds=end / sample_rate,
                        speech_probability=float(avg_prob),
                    )
                )

        return segments

    def contains_speech(self, audio: np.ndarray, sample_rate: int = 16000) -> bool:
        """Quick check: does the audio contain any speech?"""
        segments = self.detect_speech_segments(audio, sample_rate)
        return len(segments) > 0

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded,
            "model_path": self.config.model_path,
            "load_time_ms": self._load_time_ms,
            "sample_rate": self.config.sample_rate,
            "speech_threshold": self.config.speech_threshold,
        }
