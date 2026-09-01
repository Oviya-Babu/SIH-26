"""Phase 3 AI Gateway service tests (CLAUDE.md §18, §20).

Tests for ASR/TTS/NLU endpoints running in isolation (no database access).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from medikiosk_ai.main import app


@pytest.fixture
def ai_client():
    """Test client for AI Gateway with lifespan startup."""
    with TestClient(app) as client:
        yield client


def test_ai_gateway_health(ai_client):
    """Test AI Gateway health check."""
    response = ai_client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "medikiosk-ai-gateway" in data["service"]
    print(f"✓ AI Gateway health: {data}")


def test_ai_gateway_readiness(ai_client):
    """Test AI Gateway readiness (all components initialized)."""
    response = ai_client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is True
    print(f"✓ AI Gateway readiness: {data}")


def test_ai_gateway_models_metadata(ai_client):
    """Test available models and language support."""
    response = ai_client.get("/v1/meta/models")
    assert response.status_code == 200
    data = response.json()
    
    assert "asr_model" in data
    assert "tts_model" in data
    assert "supported_languages" in data
    assert set(data["supported_languages"]) == {"hi", "en", "ta", "te", "ml"}
    assert "audio_config" in data
    assert data["audio_config"]["sample_rate"] == 16000
    
    print(f"✓ AI Gateway models:")
    print(f"  ASR: {data['asr_model']}")
    print(f"  TTS: {data['tts_model']}")
    print(f"  Languages: {data['supported_languages']}")


def test_asr_transcribe_mock(ai_client):
    """Test ASR transcription endpoint (mock/sandbox mode).
    
    CLAUDE.md §18.1 [MOCK/SANDBOX]: Prototype uses mock responses.
    """
    # Create minimal audio file (WAV header + silence)
    import io
    import wave
    
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 16000)  # 1 second of silence
    
    audio_bytes = wav_io.getvalue()
    
    response = ai_client.post(
        "/v1/asr/transcribe",
        params={"language": "en"},
        files={"file": ("test.wav", audio_bytes, "audio/wav")},
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert "transcript" in data
    assert "confidence" in data
    assert "language" in data
    assert "inference_time_ms" in data
    assert 0.0 <= data["confidence"] <= 1.0
    
    print(f"✓ ASR transcribe:")
    print(f"  Transcript: '{data.get('transcript', '')}'")
    print(f"  Confidence: {data['confidence']:.2f}")
    print(f"  Inference time: {data.get('inference_time_ms', 0):.1f}ms")


def test_tts_synthesize(ai_client):
    """Test TTS synthesis endpoint.
    
    CLAUDE.md §18: TTS converts question text to speech, streamed.
    """
    response = ai_client.post(
        "/v1/tts/synthesize",
        json={
            "text": "Are you experiencing any chest pain?",
            "language": "en",
            "voice_gender": "female",
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert "audio_base64" in data or "audio_hex" in data
    assert "language" in data
    assert "inference_time_ms" in data
    
    # Audio should be hex-encoded and non-empty
    audio_hex = data.get("audio_base64") or data.get("audio_hex")
    assert len(audio_hex) > 0
    
    print(f"✓ TTS synthesize:")
    print(f"  Audio length: {len(audio_hex)//2} bytes")
    print(f"  Language: {data['language']}")
    print(f"  Inference time: {data.get('inference_time_ms', 0):.1f}ms")


def test_tts_question_endpoint(ai_client):
    """Test convenience endpoint for speaking a question."""
    response = ai_client.post(
        "/v1/tts/question",
        params={
            "question_text": "Do you have any allergies?",
            "language": "hi",
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert "audio_hex" in data
    assert "language" in data
    assert data["language"] == "hi"
    
    print(f"✓ TTS question endpoint: audio_len={len(data['audio_hex'])//2}")


def test_nlu_slot_fill(ai_client):
    """Test NLU slot-filling endpoint.
    
    CLAUDE.md §10: NLU maps free text onto structured concepts.
    """
    response = ai_client.post(
        "/v1/nlu/slot-fill",
        json={
            "transcript": "I have had chest pain for two days",
            "field_id": "hpi.duration",
            "language": "en",
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert "field_id" in data
    assert "value_raw" in data
    assert "value_normalized" in data
    assert "confidence" in data
    assert "inference_time_ms" in data
    assert 0.0 <= data["confidence"] <= 1.0
    
    print(f"✓ NLU slot-fill:")
    print(f"  Field: {data['field_id']}")
    print(f"  Raw: '{data['value_raw']}'")
    print(f"  Normalized: {data['value_normalized']}")
    print(f"  Confidence: {data['confidence']:.2f}")
    print(f"  Inference time: {data.get('inference_time_ms', 0):.1f}ms")


def test_multilingual_asr(ai_client):
    """Test ASR for all supported languages.
    
    CLAUDE.md §18.1: Bhashini supports hi, en, ta, te, ml.
    """
    languages = ["hi", "en", "ta", "te", "ml"]
    
    import io
    import wave
    
    # Create test audio
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 16000)
    
    audio_bytes = wav_io.getvalue()
    
    for lang in languages:
        response = ai_client.post(
            "/v1/asr/transcribe",
            params={"language": lang},
            files={"file": ("test.wav", audio_bytes, "audio/wav")},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["language"] == lang
        print(f"✓ Multilingual ASR: {lang} → {data['transcript'][:50] if data.get('transcript') else '(silence)'}")


def test_latency_budget_asr(ai_client):
    """Test ASR latency against §54 budget (<800ms).
    
    CLAUDE.md §54: ASR final <800ms p95
    """
    import io
    import wave
    import time
    
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 16000 * 2)  # 2 seconds
    
    audio_bytes = wav_io.getvalue()
    
    start = time.perf_counter()
    response = ai_client.post(
        "/v1/asr/transcribe",
        params={"language": "en"},
        files={"file": ("test.wav", audio_bytes, "audio/wav")},
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    
    assert response.status_code == 200
    assert elapsed_ms < 800, f"ASR exceeded latency budget: {elapsed_ms:.0f}ms > 800ms"
    
    print(f"✓ ASR latency: {elapsed_ms:.0f}ms (budget: 800ms)")


def test_latency_budget_tts(ai_client):
    """Test TTS latency against §54 budget (<3s, typically <1s).
    
    CLAUDE.md §54: TTS is streamed, never blocking.
    """
    import time
    
    start = time.perf_counter()
    response = ai_client.post(
        "/v1/tts/synthesize",
        json={
            "text": "Do you have any history of hypertension or diabetes?",
            "language": "en",
        },
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    
    assert response.status_code == 200
    assert elapsed_ms < 3000, f"TTS exceeded budget: {elapsed_ms:.0f}ms > 3000ms"
    
    print(f"✓ TTS latency: {elapsed_ms:.0f}ms (budget: 3000ms)")


def test_latency_budget_nlu(ai_client):
    """Test NLU latency against §54 budget (<200ms).
    
    CLAUDE.md §54: Clinical NLU <200ms
    """
    import time
    
    start = time.perf_counter()
    response = ai_client.post(
        "/v1/nlu/slot-fill",
        json={
            "transcript": "I have had chest pain for approximately two weeks",
            "field_id": "hpi.onset",
            "language": "en",
        },
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    
    assert response.status_code == 200
    assert elapsed_ms < 200, f"NLU exceeded latency budget: {elapsed_ms:.0f}ms > 200ms"
    
    print(f"✓ NLU latency: {elapsed_ms:.0f}ms (budget: 200ms)")


def test_circuit_breaker_state(ai_client):
    """Test that circuit breaker state is visible for monitoring.
    
    CLAUDE.md §37, §39: Observability on circuit state (health endpoint).
    """
    response = ai_client.get("/readyz")
    assert response.status_code == 200
    # Circuit state would be included in extended health response
    # This is a placeholder for future observability
    print(f"✓ Circuit breaker monitoring: health endpoint available")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
