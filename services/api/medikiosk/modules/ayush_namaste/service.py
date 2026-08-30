"""NAMASTE / ICD-11 TM2 dual coding (CLAUDE.md §12, §22, §24).

    AI suggests ranked candidates  →  a practitioner CONFIRMS
                                   →  only a confirmed mapping is written and exported

[RED LINE §10] AI never auto-assigns a diagnosis code. This module can rank and
can record a confirmation; it has no path that writes a mapping without a
practitioner's identity attached, and ``namaste_mapping.confirmed_by`` is NOT NULL
in the schema so the database refuses one too.

[ASSUMPTION §24, §63] NAMASTE / ICD-11 TM2 live API access terms are NOT confirmed
with the Ministry's AYUSH Grid team. This build therefore uses a **static,
versioned snapshot**, labelled as such in every response. It is never presented as
live, and ``terminology_source`` records which it was.

Both interview-derived and document-derived diagnosis facts route through this
same code path — one path, not two (§17.2, §24).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.ai.gateway_client import AIGatewayClient
from medikiosk.db import Principal
from medikiosk.errors import Conflict, DependencyUnavailable, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

SNAPSHOT_LABEL = (
    "STATIC VERSIONED SNAPSHOT — not a live Ministry API. "
    "Access terms are unconfirmed (CLAUDE.md §24 [ASSUMPTION])."
)


@dataclass(frozen=True, slots=True)
class TerminologyEntry:
    namaste_code: str
    namaste_term: str
    namaste_system: str
    icd11_tm2_code: str | None
    icd11_tm2_term: str | None
    icd11_biomed_code: str | None
    synonyms: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Suggestion:
    entry: TerminologyEntry
    score: float
    rank: int
    matched_on: str


class TerminologyRegistry:
    """Loads the governed terminology snapshot."""

    def __init__(self, content_root: Path) -> None:
        self._root = content_root / "terminology"
        self._cache: dict[str, tuple[str, tuple[TerminologyEntry, ...]]] = {}

    def load(self, version: str) -> tuple[str, tuple[TerminologyEntry, ...]]:
        cached = self._cache.get(version)
        if cached is not None:
            return cached

        path = self._root / f"{version}.json"
        if not path.is_file():
            raise NotFound(
                f"terminology snapshot not found: {version}",
                reason_code="terminology_snapshot_missing",
            )
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)

        entries = tuple(
            TerminologyEntry(
                namaste_code=str(e["namaste_code"]),
                namaste_term=str(e["namaste_term"]),
                namaste_system=str(e.get("namaste_system", "ayurveda")),
                icd11_tm2_code=e.get("icd11_tm2_code"),
                icd11_tm2_term=e.get("icd11_tm2_term"),
                icd11_biomed_code=e.get("icd11_biomed_code"),
                synonyms=tuple(e.get("synonyms", ()) or ()),
                keywords=tuple(k.lower() for k in (e.get("keywords", ()) or ())),
            )
            for e in document.get("entries", [])
        )
        if not entries:
            raise ValidationFailed(
                "terminology snapshot contains no entries",
                reason_code="terminology_snapshot_empty",
            )
        self._cache[version] = (checksum, entries)
        log.info(
            "terminology_snapshot_loaded",
            component="ayush_namaste",
            terminology_version=version,
            count=len(entries),
        )
        return checksum, entries


def _deterministic_candidates(
    entries: tuple[TerminologyEntry, ...],
    text: str,
    *,
    limit: int = 12,
) -> list[Suggestion]:
    """Keyword/synonym matching over the snapshot.

    Runs FIRST and always. The model, when available, only re-ranks this list, so
    a suggestion can never be a code the snapshot does not contain — the model
    cannot invent a diagnosis code even if it tries.
    """
    needle = text.lower().strip()
    tokens = {t for t in needle.replace(",", " ").split() if len(t) > 2}
    scored: list[tuple[float, str, TerminologyEntry]] = []

    for entry in entries:
        if needle and needle == entry.namaste_term.lower():
            scored.append((1.0, "exact_term", entry))
            continue
        if needle and any(needle == s.lower() for s in entry.synonyms):
            scored.append((0.95, "exact_synonym", entry))
            continue

        hits = sum(1 for keyword in entry.keywords if keyword in needle)
        token_hits = sum(1 for token in tokens if any(token in k for k in entry.keywords))
        if hits or token_hits:
            score = min(0.9, 0.45 + 0.15 * hits + 0.08 * token_hits)
            scored.append((score, "keyword", entry))

    scored.sort(key=lambda item: (-item[0], item[2].namaste_code))
    return [
        Suggestion(entry=entry, score=score, rank=index + 1, matched_on=matched)
        for index, (score, matched, entry) in enumerate(scored[:limit])
    ]


async def suggest(
    conn: asyncpg.Connection,
    ai: AIGatewayClient | None,
    registry: TerminologyRegistry,
    *,
    fact_id: UUID,
    version: str,
    language: str = "en",
) -> dict[str, Any]:
    """Suggest ranked NAMASTE + ICD-11 TM2 candidates for a diagnosis fact.

    Suggestions are NOT persisted. Nothing enters ``namaste_mapping`` until a
    practitioner confirms one (§24).
    """
    fact = await conn.fetchrow(
        """
        SELECT id, session_id, category, concept_code, value_normalized, value_raw,
               source_type
          FROM clinical_fact
         WHERE id = $1 AND superseded_by IS NULL
        """,
        fact_id,
    )
    if fact is None:
        raise NotFound("fact not found", reason_code="not_found")
    if fact["category"] != "diagnosis":
        raise ValidationFailed(
            "only diagnosis facts are coded", reason_code="not_a_diagnosis_fact"
        )

    checksum, entries = registry.load(version)
    text = _diagnosis_text(fact)
    candidates = _deterministic_candidates(entries, text)

    ai_used = False
    if ai is not None and candidates:
        try:
            ranked = await ai.suggest_terminology(
                diagnosis_text=text,
                language=language,
                system="namaste_icd11_tm2",
                candidates=[
                    {
                        "namaste_code": s.entry.namaste_code,
                        "namaste_term": s.entry.namaste_term,
                        "icd11_tm2_code": s.entry.icd11_tm2_code,
                    }
                    for s in candidates
                ],
            )
            # Re-rank ONLY within the deterministic candidate set. A code the
            # model returns that is not already a candidate is discarded.
            by_code = {s.entry.namaste_code: s for s in candidates}
            reordered: list[Suggestion] = []
            for index, item in enumerate(ranked):
                match = by_code.get(str(item.get("namaste_code")))
                if match is None:
                    continue
                reordered.append(
                    Suggestion(
                        entry=match.entry,
                        score=float(item.get("score", match.score)),
                        rank=index + 1,
                        matched_on="ai_reranked",
                    )
                )
            if reordered:
                candidates = reordered
                ai_used = True
        except DependencyUnavailable:
            # Deterministic ranking stands. Coding is never blocked by the model.
            log.info(
                "terminology_rank_unavailable",
                component="ayush_namaste",
                fallback_engaged=True,
            )

    return {
        "fact_id": str(fact_id),
        "session_id": str(fact["session_id"]),
        "diagnosis_text": text,
        "terminology_source": SNAPSHOT_LABEL,
        "terminology_version": version,
        "snapshot_checksum": checksum[:12],
        "is_live_api": False,
        "ranking": "ai_reranked" if ai_used else "deterministic_only",
        "requires_practitioner_confirmation": True,
        "candidates": [
            {
                "rank": s.rank,
                "score": round(s.score, 3),
                "matched_on": s.matched_on,
                "namaste_code": s.entry.namaste_code,
                "namaste_term": s.entry.namaste_term,
                "namaste_system": s.entry.namaste_system,
                "icd11_tm2_code": s.entry.icd11_tm2_code,
                "icd11_tm2_term": s.entry.icd11_tm2_term,
                "icd11_biomed_code": s.entry.icd11_biomed_code,
            }
            for s in candidates
        ],
    }


async def confirm(
    conn: asyncpg.Connection,
    principal: Principal,
    registry: TerminologyRegistry,
    *,
    fact_id: UUID,
    namaste_code: str,
    version: str,
    ai_suggestion_rank: int | None = None,
    ai_suggestion_score: float | None = None,
) -> dict[str, Any]:
    """Record a PRACTITIONER-CONFIRMED dual coding (§24).

    Only a mapping that reaches this function is written, and only a written
    mapping is exported. The code must exist in the governed snapshot: a
    free-text code cannot be smuggled in.
    """
    if principal.role not in ("physician", "ayush_practitioner"):
        raise Conflict(
            "only a practitioner may confirm a diagnosis coding",
            reason_code="practitioner_required",
        )

    checksum, entries = registry.load(version)
    entry = next((e for e in entries if e.namaste_code == namaste_code), None)
    if entry is None:
        raise ValidationFailed(
            "code is not present in the governed terminology snapshot",
            reason_code="unknown_terminology_code",
        )

    fact = await conn.fetchrow(
        """
        SELECT id, session_id, category FROM clinical_fact
         WHERE id = $1 AND superseded_by IS NULL
        """,
        fact_id,
    )
    if fact is None:
        raise NotFound("fact not found", reason_code="not_found")
    if fact["category"] != "diagnosis":
        raise ValidationFailed("only diagnosis facts are coded",
                               reason_code="not_a_diagnosis_fact")

    mapping_id = await conn.fetchval(
        """
        INSERT INTO namaste_mapping
            (tenant_id, session_id, fact_id, namaste_code, namaste_term, namaste_system,
             icd11_tm2_code, icd11_tm2_term, icd11_biomed_code,
             terminology_source, terminology_version, confirmed_by,
             ai_suggestion_rank, ai_suggestion_score)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (fact_id, namaste_code) DO NOTHING
        RETURNING id
        """,
        principal.tenant_id,
        fact["session_id"],
        fact_id,
        entry.namaste_code,
        entry.namaste_term,
        entry.namaste_system,
        entry.icd11_tm2_code,
        entry.icd11_tm2_term,
        entry.icd11_biomed_code,
        "static_snapshot",
        version,
        principal.actor_id,
        ai_suggestion_rank,
        ai_suggestion_score,
    )
    if mapping_id is None:
        raise Conflict("this coding is already confirmed", reason_code="already_confirmed")

    await audit.record(
        conn,
        principal,
        action="namaste.coding_confirmed",
        entity_type="namaste_mapping",
        entity_id=mapping_id,
        detail={
            "namaste_code": entry.namaste_code,
            "icd11_tm2_code": entry.icd11_tm2_code,
            "terminology_version": version,
            "count": ai_suggestion_rank,
        },
    )
    return {
        "mapping_id": str(mapping_id),
        "fact_id": str(fact_id),
        "namaste_code": entry.namaste_code,
        "namaste_term": entry.namaste_term,
        "icd11_tm2_code": entry.icd11_tm2_code,
        "icd11_tm2_term": entry.icd11_tm2_term,
        "dual_coded": entry.icd11_tm2_code is not None,
        "terminology_version": version,
        "snapshot_checksum": checksum[:12],
        "confirmed_by_role": principal.role,
    }


async def confirmed_for_session(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT m.id, m.fact_id, m.namaste_code, m.namaste_term, m.namaste_system,
               m.icd11_tm2_code, m.icd11_tm2_term, m.icd11_biomed_code,
               m.terminology_source, m.terminology_version, m.confirmed_at,
               u.display_name AS confirmed_by_name, u.role AS confirmed_by_role
          FROM namaste_mapping m
          JOIN app_user u ON u.id = m.confirmed_by
         WHERE m.session_id = $1
         ORDER BY m.confirmed_at
        """,
        session_id,
    )
    return [
        {
            **dict(r),
            "dual_coded": r["icd11_tm2_code"] is not None,
            "is_live_api": False,
        }
        for r in rows
    ]


async def uncoded_diagnoses(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    """Diagnosis facts still awaiting practitioner coding."""
    rows = await conn.fetch(
        """
        SELECT f.id, f.concept_code, f.value_normalized, f.value_raw, f.source_type
          FROM clinical_fact f
         WHERE f.session_id = $1
           AND f.superseded_by IS NULL
           AND f.category = 'diagnosis'
           AND NOT EXISTS (SELECT 1 FROM namaste_mapping m WHERE m.fact_id = f.id)
         ORDER BY f.created_at
        """,
        session_id,
    )
    return [dict(r) for r in rows]


def _diagnosis_text(fact) -> str:
    value = fact["value_normalized"]
    if isinstance(value, dict):
        for key in ("text", "code", "value"):
            if key in value:
                return str(value[key])
    if fact["value_raw"]:
        return str(fact["value_raw"])
    return str(fact["concept_code"])
