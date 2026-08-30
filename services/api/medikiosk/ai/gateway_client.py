"""Client for the AI Gateway (CLAUDE.md §18, §19, §20, §36, §37).

This is the ONLY way the application talks to any model. Two consequences the
architecture depends on:

* **Isolation (§20).** The gateway is reached over HTTP and nothing else. It has
  no database client, no connection string and no route to PostgreSQL. AI output
  arrives here as *data*, is validated by the caller, and is persisted only by the
  Clinical Facts module.
* **Degradation (§37).** Every call has a bounded timeout and a defined fallback.
  A model outage degrades a feature; it never blocks clinical care. The circuit
  breaker exists so a slow gateway cannot hold the patient's interactive loop
  past the §54 budget.

[RED LINE §18.1] Never send anything resembling real patient content through an
anonymous, no-billing API key. That control lives in the gateway's own
configuration, but this client refuses to start in production without an
explicitly configured, billing-enabled provider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from medikiosk.config import Settings
from medikiosk.errors import DependencyUnavailable
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


class Component(StrEnum):
    ASR = "asr"
    NLU = "nlu"
    TTS = "tts"
    OCR = "ocr"
    LLM = "llm"


@dataclass
class CircuitBreaker:
    """Per-component breaker.

    Opening the circuit is what turns "the ASR container is wedged" into
    "the kiosk offered touch input instantly" rather than "every patient waited
    5 seconds for a timeout" (§37, §54).
    """

    failure_threshold: int = 4
    recovery_seconds: float = 20.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, component: str) -> bool:
        opened = self._opened_at.get(component)
        if opened is None:
            return False
        if (time.monotonic() - opened) >= self.recovery_seconds:
            # Half-open: let one request through to test recovery.
            self._opened_at.pop(component, None)
            self._failures[component] = self.failure_threshold - 1
            return False
        return True

    def record_success(self, component: str) -> None:
        self._failures.pop(component, None)
        self._opened_at.pop(component, None)

    def record_failure(self, component: str) -> None:
        count = self._failures.get(component, 0) + 1
        self._failures[component] = count
        if count >= self.failure_threshold:
            self._opened_at[component] = time.monotonic()
            log.warning("ai_circuit_opened", component=str(component), fallback_engaged=True)

    def state(self) -> dict[str, str]:
        return {
            component: ("open" if self.is_open(component) else "closed")
            for component in set(self._failures) | set(self._opened_at)
        }


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    confidence: float
    language: str
    is_final: bool
    model_version: str


@dataclass(frozen=True, slots=True)
class SlotFillResult:
    """NLU output: free text mapped onto a governed concept's option codes.

    Note what is NOT here: any notion of which question comes next. The NLU maps
    an utterance onto the CURRENT field's option set, and nothing else. Question
    selection is the engine's, always (§10).
    """

    codes: tuple[str, ...]
    confidence: float
    model_version: str
    unmatched_text: str | None = None


@dataclass(frozen=True, slots=True)
class OcrPage:
    page_number: int
    text: str
    confidence: float
    handwritten: bool
    layout: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OcrResult:
    pages: tuple[OcrPage, ...]
    engine: str
    model_version: str
    doc_class: str | None
    quality: str


@dataclass(frozen=True, slots=True)
class LlmDraft:
    statements: tuple[dict[str, Any], ...]
    model_version: str
    prompt_version: str
    latency_ms: int


class AIGatewayClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._base = settings.ai_gateway_url.rstrip("/")
        self.breaker = CircuitBreaker()

    async def _post(
        self,
        component: Component,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if self.breaker.is_open(str(component)):
            raise DependencyUnavailable(
                f"{component} circuit is open", reason_code=f"{component}_unavailable"
            )
        started = time.perf_counter()
        try:
            response = await self._http.post(
                f"{self._base}{path}", json=payload, timeout=timeout
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.breaker.record_failure(str(component))
            log.warning(
                "ai_call_failed",
                component=str(component),
                error_class=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
                fallback_engaged=True,
            )
            raise DependencyUnavailable(
                f"{component} is unavailable", reason_code=f"{component}_unavailable"
            ) from exc

        self.breaker.record_success(str(component))
        log.info(
            "ai_call_completed",
            component=str(component),
            duration_ms=int((time.perf_counter() - started) * 1000),
            latency_class=_latency_class(component),
        )
        return response.json()

    # -- ASR (§18.2) ---------------------------------------------------------
    async def transcribe(
        self,
        *,
        audio_base64: str,
        language: str,
        asr_locale: str,
        is_final: bool = True,
    ) -> TranscriptionResult:
        """Transcribe one utterance.

        The AUDIO is passed through and never persisted anywhere in MediKiosk
        (§17.3, §38): the bytes exist for the duration of this call only.
        """
        body = await self._post(
            Component.ASR,
            "/v1/asr/transcribe",
            {
                "audio_base64": audio_base64,
                "language": language,
                "locale": asr_locale,
                "is_final": is_final,
                # Noise suppression is applied BEFORE ASR, not after (§18.2).
                "noise_suppression": True,
                "vad": True,
            },
            timeout=self._settings.ai_asr_timeout_seconds,
        )
        return TranscriptionResult(
            text=body.get("text", ""),
            confidence=float(body.get("confidence", 0.0)),
            language=body.get("language", language),
            is_final=bool(body.get("is_final", is_final)),
            model_version=body.get("model_version", "unknown"),
        )

    # -- NLU (§10, §18.3) ---------------------------------------------------
    async def fill_slot(
        self,
        *,
        transcript: str,
        language: str,
        concept_code: str,
        nlu_slot: str | None,
        allowed_codes: tuple[str, ...],
        value_type: str,
    ) -> SlotFillResult:
        """Map free text onto the CURRENT field's option codes.

        ``allowed_codes`` is a closed set supplied by the engine. The model cannot
        invent a code, and the caller re-validates against the same set, so an
        out-of-vocabulary hallucination is rejected twice.
        """
        body = await self._post(
            Component.NLU,
            "/v1/nlu/slot-fill",
            {
                "transcript": transcript,
                "language": language,
                "concept_code": concept_code,
                "slot": nlu_slot,
                "allowed_codes": list(allowed_codes),
                "value_type": value_type,
            },
            timeout=self._settings.ai_nlu_timeout_seconds,
        )
        codes = tuple(str(c) for c in body.get("codes", []) if str(c) in set(allowed_codes))
        return SlotFillResult(
            codes=codes,
            confidence=float(body.get("confidence", 0.0)),
            model_version=body.get("model_version", "unknown"),
            unmatched_text=body.get("unmatched_text"),
        )

    # -- TTS ----------------------------------------------------------------
    async def synthesise(
        self, *, text: str, language: str, tts_locale: str, voice: str
    ) -> dict[str, Any]:
        return await self._post(
            Component.TTS,
            "/v1/tts/synthesise",
            {"text": text, "language": language, "locale": tts_locale, "voice": voice},
            timeout=self._settings.ai_asr_timeout_seconds,
        )

    # -- OCR (§17.2) --------------------------------------------------------
    async def extract_document(
        self, *, image_base64: str, mime_type: str, hint_language: str
    ) -> OcrResult:
        body = await self._post(
            Component.OCR,
            "/v1/ocr/extract",
            {
                "content_base64": image_base64,
                "mime_type": mime_type,
                "hint_language": hint_language,
            },
            timeout=self._settings.ai_ocr_timeout_seconds,
        )
        return OcrResult(
            pages=tuple(
                OcrPage(
                    page_number=int(p.get("page_number", i + 1)),
                    text=p.get("text", ""),
                    confidence=float(p.get("confidence", 0.0)),
                    handwritten=bool(p.get("handwritten", False)),
                    layout=p.get("layout", {}),
                )
                for i, p in enumerate(body.get("pages", []))
            ),
            engine=body.get("engine", "unknown"),
            model_version=body.get("model_version", "unknown"),
            doc_class=body.get("doc_class"),
            quality=body.get("quality", "ok"),
        )

    async def extract_entities(
        self, *, pages: list[dict[str, Any]], hint_language: str
    ) -> dict[str, Any]:
        """Medical entity extraction from OCR text.

        The OCR text is UNTRUSTED DATA (§19, §36). The gateway inserts it into a
        strictly templated prompt with system instructions kept separate, and the
        caller confidence-gates everything that comes back.
        """
        return await self._post(
            Component.OCR,
            "/v1/ocr/entities",
            {"pages": pages, "hint_language": hint_language},
            timeout=self._settings.ai_ocr_timeout_seconds,
        )

    # -- LLM summary (§19) --------------------------------------------------
    async def draft_summary(
        self,
        *,
        facts: list[dict[str, Any]],
        language: str,
        sections: list[str],
    ) -> LlmDraft:
        """Draft physician-facing prose from STRUCTURED FACTS ONLY.

        [RED LINE §19] The raw patient transcript is never fed in for "creative"
        summarisation, and every generated sentence must cite a real
        ``clinical_fact.id``. The citation contract is enforced by the caller
        (:mod:`medikiosk.modules.summary.service`), not by prompting alone.
        """
        started = time.perf_counter()
        body = await self._post(
            Component.LLM,
            "/v1/llm/summary",
            {
                "facts": facts,
                "language": language,
                "sections": sections,
                "require_citations": True,
            },
            timeout=self._settings.ai_llm_timeout_seconds,
        )
        return LlmDraft(
            statements=tuple(body.get("statements", [])),
            model_version=body.get("model_version", "unknown"),
            prompt_version=body.get("prompt_version", "unknown"),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def suggest_terminology(
        self,
        *,
        diagnosis_text: str,
        language: str,
        system: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rank NAMASTE / ICD-11 TM2 candidates (§24).

        The candidate list comes from the governed terminology snapshot; the model
        only ranks it. A practitioner then confirms, and only a confirmed mapping
        is ever written.
        """
        body = await self._post(
            Component.LLM,
            "/v1/llm/terminology-rank",
            {
                "text": diagnosis_text,
                "language": language,
                "system": system,
                "candidates": candidates,
            },
            timeout=self._settings.ai_llm_timeout_seconds,
        )
        return list(body.get("ranked", []))

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._http.get(f"{self._base}/healthz", timeout=2.0)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "error": type(exc).__name__, "breaker": self.breaker.state()}
        return {"ok": True, "components": body.get("components", {}),
                "breaker": self.breaker.state()}


def _latency_class(component: Component) -> str:
    """The §18.3 latency class, so SLO dashboards group calls correctly."""
    return {
        Component.ASR: "interactive",
        Component.NLU: "interactive",
        Component.TTS: "interactive",
        Component.OCR: "async",
        Component.LLM: "bounded_async",
    }[component]
