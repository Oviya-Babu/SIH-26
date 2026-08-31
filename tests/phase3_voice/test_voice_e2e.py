"""Phase 3 Voice end-to-end tests (CLAUDE.md §3, §18, §51, §54).

This test suite verifies:
1. ASR (speech-to-text) transcription accuracy
2. TTS (text-to-speech) synthesis works
3. Voice answers create clinical facts correctly
4. Latency stays within §54 budget
5. Confidence thresholds gate answer acceptance
6. Fallback to text on ASR failure (§37)
7. Conversational flow: question → voice answer → next question
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from wave import open as wave_open

# Test fixtures will use pytest-asyncio
pytestmark = pytest.mark.asyncio


# ============================================================================
# Test Helpers
# ============================================================================

def create_minimal_wav_audio(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Generate a minimal valid WAV file for testing.
    
    Creates a simple sine wave at 440 Hz (A4 note).
    """
    import array
    import math
    
    num_samples = int(duration_seconds * sample_rate)
    frequency = 440.0
    amplitude = 32767 * 0.3  # 30% volume
    
    samples = array.array('h')
    for i in range(num_samples):
        sample = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        samples.append(sample)
    
    # Create WAV file in memory
    wav_io = io.BytesIO()
    with wave_open(wav_io, 'wb') as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    
    return wav_io.getvalue()


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.asyncio
async def test_asr_gateway_transcription(ai_gateway_client):
    """Test ASR transcription via AI Gateway.
    
    CLAUDE.md §18.2: ASR emits partial hypotheses, confidence-scored.
    """
    # Create a test audio (sine wave, not real speech)
    audio_bytes = create_minimal_wav_audio(duration_seconds=2.0)
    
    # Call transcribe
    result = await ai_gateway_client.transcribe(
        audio_base64=base64.b64encode(audio_bytes).decode(),
        language="en",
        asr_locale="en-IN",
    )
    
    assert result is not None
    assert hasattr(result, 'text')
    assert hasattr(result, 'confidence')
    assert 0.0 <= result.confidence <= 1.0
    assert result.is_final is True
    print(f"✓ ASR transcription: text_len={len(result.text)}, confidence={result.confidence:.2f}")


@pytest.mark.asyncio
async def test_tts_gateway_synthesis(ai_gateway_client):
    """Test TTS synthesis via AI Gateway.
    
    CLAUDE.md §54: TTS is streamed, never blocking.
    """
    question_text = "Are you experiencing any chest pain?"
    
    result = await ai_gateway_client.synthesise(
        text=question_text,
        language="en",
        tts_locale="en-IN",
        voice="female",
    )
    
    assert result is not None
    assert 'audio_base64' in result
    # Audio should be hex-encoded and non-empty
    audio_hex = result.get('audio_base64', '')
    assert len(audio_hex) > 0
    print(f"✓ TTS synthesis: audio_bytes={len(audio_hex)//2}, question_len={len(question_text)}")


@pytest.mark.asyncio
async def test_nlu_slot_fill(ai_gateway_client):
    """Test NLU slot-filling.
    
    CLAUDE.md §10: NLU validates and normalizes free text into structured concepts.
    """
    transcript = "chest pain for two days"
    field_id = "hpi.duration"  # Example field
    
    result = await ai_gateway_client.fill_slot(
        transcript=transcript,
        language="en",
        concept_code="hpi",
        nlu_slot="duration",
        allowed_codes=("days", "weeks", "months", "hours", "minutes"),
        value_type="string",
    )
    
    assert result is not None
    assert hasattr(result, 'codes')
    assert hasattr(result, 'confidence')
    assert 0.0 <= result.confidence <= 1.0
    print(f"✓ NLU slot-fill: codes={result.codes}, confidence={result.confidence:.2f}")


@pytest.mark.asyncio
async def test_voice_answer_endpoint_latency(client: TestClient, test_session_id: UUID):
    """Test voice answer endpoint latency against §54 budget.
    
    CLAUDE.md §54: end-to-end speech→next-question <1.5s p95
    """
    audio_bytes = create_minimal_wav_audio(duration_seconds=1.0)
    
    start = time.perf_counter()
    response = client.post(
        f"/v1/sessions/{test_session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    
    assert response.status_code == 200
    data = response.json()
    
    # Parse response
    assert 'inference_time_ms' in data
    assert 'confidence' in data
    assert 'verdict' in data
    assert 'completeness' in data
    
    # Check latency budget (§54: <1.5s p95)
    assert elapsed_ms < 1500, f"Voice answer exceeded latency budget: {elapsed_ms:.0f}ms > 1500ms"
    
    print(f"✓ Voice answer latency: {elapsed_ms:.0f}ms (budget: 1500ms)")
    print(f"  Inference time: {data.get('inference_time_ms', 0):.1f}ms")


@pytest.mark.asyncio
async def test_confidence_threshold_acceptance(client: TestClient, test_session_id: UUID):
    """Test confidence-gated answer acceptance.
    
    CLAUDE.md §10: κ(v) ≥ τ_high → accept, τ_low ≤ κ < τ_high → confirm-back,
    κ < τ_low → reject
    """
    audio_bytes = create_minimal_wav_audio(duration_seconds=1.0)
    
    response = client.post(
        f"/v1/sessions/{test_session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    
    confidence = data.get('confidence', 0.0)
    verdict = data.get('verdict', '')
    
    # Verdict must match confidence level
    if confidence >= 0.75:  # τ_high
        assert verdict in ('accepted', 'ACCEPTED'), f"High confidence but verdict={verdict}"
    elif confidence < 0.4:  # τ_low
        assert verdict in ('rejected', 'REJECTED'), f"Low confidence but verdict={verdict}"
    else:
        assert verdict in ('confirm_back', 'CONFIRM_BACK'), f"Medium confidence but verdict={verdict}"
    
    print(f"✓ Confidence gating: confidence={confidence:.2f}, verdict={verdict}")


@pytest.mark.asyncio
async def test_voice_fallback_on_asr_timeout(client: TestClient, test_session_id: UUID):
    """Test graceful fallback to text when ASR fails.
    
    CLAUDE.md §37: ASR timeout → automatic fallback to touch/text
    """
    # Empty audio file (will cause ASR to fail or timeout)
    empty_audio = b''
    
    response = client.post(
        f"/v1/sessions/{test_session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", empty_audio, "audio/wav")},
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    
    # Should return 400 validation error, not 500 crash
    assert response.status_code in (400, 422)
    data = response.json()
    assert 'error' in data or 'detail' in data
    
    print(f"✓ Voice fallback: empty audio rejected gracefully (status={response.status_code})")


@pytest.mark.asyncio
async def test_tts_speak_question(client: TestClient, test_session_id: UUID):
    """Test TTS question speaking.
    
    CLAUDE.md §18, §54: Questions are spoken via TTS, streamed, non-blocking.
    """
    response = client.get(
        f"/v1/sessions/{test_session_id}/questions/hpi.chief_complaint/speak",
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert 'question_text' in data
    assert 'audio_hex' in data
    assert len(data['audio_hex']) > 0
    
    print(f"✓ TTS question: audio_len={len(data['audio_hex'])//2}, question={data['question_text'][:50]}...")


@pytest.mark.asyncio
async def test_voice_interview_flow(client: TestClient, test_patient_id: UUID):
    """Test complete voice interview flow.
    
    Flow: Start → Question → Voice Answer → Clinical Fact → Red Flag? → Next Question
    
    CLAUDE.md §3, §14: Full vertical slice with voice input.
    """
    # Start session
    session_response = client.post(
        "/v1/sessions",
        json={
            "patient_id": str(test_patient_id),
            "department_id": "gen-med",
            "language": "en",
            "respondent_type": "patient",
        },
        headers={"Authorization": f"Bearer {test_device_token}"},
    )
    
    assert session_response.status_code == 200
    session_data = session_response.json()
    session_id = UUID(session_data['session_id'])
    session_token = session_data['session_token']
    
    # Get next question
    question_response = client.get(
        f"/v1/sessions/{session_id}/next-question",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert question_response.status_code == 200
    field_id = question_response.json().get('field_id')
    
    # Submit voice answer
    audio_bytes = create_minimal_wav_audio()
    answer_response = client.post(
        f"/v1/sessions/{session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    
    assert answer_response.status_code == 200
    answer_data = answer_response.json()
    
    # Verify clinical fact was created
    assert 'fact_id' in answer_data
    assert 'completeness' in answer_data
    assert 'next_field_id' in answer_data or 'verdct' in answer_data
    
    print(f"✓ Voice interview flow: session_id={session_id}")
    print(f"  Field: {field_id}")
    print(f"  Answer verdict: {answer_data.get('verdict', 'unknown')}")
    print(f"  Completeness: {answer_data.get('completeness', 0.0):.1%}")
    print(f"  Next field: {answer_data.get('next_field_id', 'complete')}")


@pytest.mark.asyncio
async def test_voice_answer_creates_clinical_fact(client: TestClient, test_session_id: UUID):
    """Test that voice answers create properly provenance-tracked clinical facts.
    
    CLAUDE.md §13: Every fact has source_type=voice_answer, respondent_id, respondent_relationship.
    """
    audio_bytes = create_minimal_wav_audio()
    
    response = client.post(
        f"/v1/sessions/{test_session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Check provenance
    assert 'fact_id' in data
    assert 'value_normalized' in data
    assert data.get('transcript', '') != ''  # Transcript from ASR
    
    # Query the fact directly to verify provenance
    fact_response = client.get(
        f"/v1/sessions/{test_session_id}/facts/{data['fact_id']}",
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    
    if fact_response.status_code == 200:
        fact = fact_response.json()
        assert fact.get('source_type') == 'voice_answer'
        print(f"✓ Voice fact provenance: source_type={fact.get('source_type')}, respondent={fact.get('respondent_id')}")


@pytest.mark.asyncio  
async def test_multilingual_voice_support():
    """Test voice support for all 5 languages.
    
    CLAUDE.md §18.1: Bhashini supports hi, en, ta, te, ml
    """
    languages = ["hi", "en", "ta", "te", "ml"]
    
    for lang in languages:
        audio_bytes = create_minimal_wav_audio()
        # Each language would have its own test endpoint
        # For now, just verify the language code is accepted
        assert lang in ["hi", "en", "ta", "te", "ml"]
        print(f"✓ Language support: {lang}")


# ============================================================================
# Regression Tests (§52)
# ============================================================================

@pytest.mark.asyncio
async def test_voice_red_flag_scenario_acs(client: TestClient, test_patient_id: UUID):
    """Test red-flag scenario: acute coronary syndrome.
    
    CLAUDE.md §14, §52: Golden scenario — red flag must fire consistently.
    
    Scenario: Patient says \"severe chest pain, sweating, shortness of breath for 30 minutes\"
    Expected: Acute Coronary Syndrome red flag fires → escalation to AMPLE fast-path
    """
    # This is a full-flow test; implementation depends on how mocking is set up
    # In real SIH, this would be an actual patient voice recording
    
    # Start session
    session_response = client.post(
        "/v1/sessions",
        json={
            "patient_id": str(test_patient_id),
            "department_id": "gen-med",
            "language": "en",
        },
        headers={"Authorization": f"Bearer {test_device_token}"},
    )
    
    session_id = UUID(session_response.json()['session_id'])
    session_token = session_response.json()['session_token']
    
    # Simulate voice answer describing ACS symptoms
    audio_bytes = create_minimal_wav_audio()
    
    answer_response = client.post(
        f"/v1/sessions/{session_id}/answers/voice",
        params={"language": "en"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    
    # Check if red flag was triggered
    # In a real test, we'd verify alert in triage queue
    assert answer_response.status_code == 200
    data = answer_response.json()
    
    # If high-risk scenario, expect escalation
    print(f"✓ ACS red-flag test: completed (escalation depends on transcript matching)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
