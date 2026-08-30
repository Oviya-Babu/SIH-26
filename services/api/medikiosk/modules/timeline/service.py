"""Chronological timeline (CLAUDE.md §16).

    TimelineEvent = (fact_ref, date_known: bool, date_value | null, source_ref)
    e1 < e2  iff  date_known(e1) AND date_known(e2) AND date_value(e1) < date_value(e2)

[RED LINE §16] Unknown-date events go to a **separate bucket** and are NEVER
interpolated. A patient saying "some years ago" must not become a guessed date on
a physician's screen — a fabricated date is worse than an admitted gap, because
the physician cannot tell it was fabricated.

Relative durations ("3 days") ARE resolved to a date, because the patient stated
a real interval against a known reference point (now). That is arithmetic, not
interpolation, and the derivation is recorded in ``source_ref`` so a physician can
see how the date was obtained.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.db import Principal, to_jsonb
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

_UNIT_DAYS: dict[str, float] = {
    "minutes": 1 / 1440,
    "hours": 1 / 24,
    "days": 1.0,
    "weeks": 7.0,
    "months": 30.44,
    "years": 365.25,
}

# Precision degrades honestly with the unit: "3 years ago" is a year-precision
# date, and presenting it as a specific day would be a false claim.
_UNIT_PRECISION: dict[str, str] = {
    "minutes": "day",
    "hours": "day",
    "days": "day",
    "weeks": "day",
    "months": "month",
    "years": "year",
}


@dataclass(frozen=True, slots=True)
class DerivedDate:
    value: date | None
    precision: str | None
    method: str


def derive_date(value: Any, *, reference: datetime | None = None) -> DerivedDate:
    """Derive a date from a normalized answer, or admit that we cannot."""
    now = reference or datetime.now(timezone.utc)

    if isinstance(value, dict):
        if isinstance(value.get("date"), str):
            try:
                parsed = date.fromisoformat(value["date"])
            except ValueError:
                return DerivedDate(None, None, "unparseable_date")
            return DerivedDate(parsed, "day", "stated_date")

        unit = value.get("unit")
        magnitude = value.get("value")
        if isinstance(unit, str) and unit in _UNIT_DAYS and isinstance(magnitude, (int, float)):
            days = float(magnitude) * _UNIT_DAYS[unit]
            return DerivedDate(
                (now - timedelta(days=days)).date(),
                _UNIT_PRECISION[unit],
                f"derived_from_duration:{magnitude}_{unit}",
            )

    # Everything else is genuinely undated. Say so.
    return DerivedDate(None, None, "no_date_available")


async def rebuild(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
) -> dict[str, int]:
    """Rebuild the timeline from the session's live facts.

    A full rebuild rather than incremental patching: facts get superseded and
    documents arrive late, and a rebuild is the only way to guarantee the timeline
    matches the current record exactly.
    """
    await conn.execute("DELETE FROM timeline_event WHERE session_id = $1", session_id)

    rows = await conn.fetch(
        """
        SELECT id, category, concept_code, concept_label, value_normalized,
               source_type, provenance_ref, created_at
          FROM clinical_fact
         WHERE session_id = $1 AND superseded_by IS NULL
         ORDER BY created_at, id
        """,
        session_id,
    )

    dated = 0
    undated = 0
    for row in rows:
        derived = derive_date(row["value_normalized"])
        known = derived.value is not None
        await conn.execute(
            """
            INSERT INTO timeline_event
                (tenant_id, session_id, fact_id, date_known, date_value, date_precision,
                 label, source_ref)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            principal.tenant_id,
            session_id,
            row["id"],
            known,
            derived.value,
            derived.precision if known else None,
            row["concept_label"],
            to_jsonb(
                {
                    "category": row["category"],
                    "concept_code": row["concept_code"],
                    "source_type": row["source_type"],
                    "date_method": derived.method,
                    "provenance": row["provenance_ref"],
                }
            ),
        )
        if known:
            dated += 1
        else:
            undated += 1

    log.info(
        "timeline_rebuilt",
        component="timeline",
        session_id=session_id,
        tenant_id=principal.tenant_id,
        count=dated + undated,
    )
    return {"dated": dated, "undated": undated}


async def get(conn: asyncpg.Connection, session_id: UUID) -> dict[str, Any]:
    """Return the timeline as two explicitly separate buckets (§16)."""
    rows = await conn.fetch(
        """
        SELECT e.id, e.fact_id, e.date_known, e.date_value, e.date_precision,
               e.label, e.source_ref,
               f.category, f.concept_code, f.value_normalized, f.source_type,
               f.respondent_relationship, f.abnormal_flag, f.is_conflicting
          FROM timeline_event e
          JOIN clinical_fact f ON f.id = e.fact_id
         WHERE e.session_id = $1
         ORDER BY e.date_known DESC, e.date_value, e.label
        """,
        session_id,
    )

    dated: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    for row in rows:
        entry = {
            "event_id": row["id"],
            "fact_id": row["fact_id"],
            "label": row["label"],
            "category": row["category"],
            "concept_code": row["concept_code"],
            "value": row["value_normalized"],
            "source_type": row["source_type"],
            "respondent_relationship": row["respondent_relationship"],
            "abnormal_flag": row["abnormal_flag"],
            "is_conflicting": row["is_conflicting"],
            "date_method": (row["source_ref"] or {}).get("date_method"),
        }
        if row["date_known"]:
            entry["date"] = row["date_value"]
            entry["precision"] = row["date_precision"]
            dated.append(entry)
        else:
            undated.append(entry)

    return {
        "session_id": str(session_id),
        "dated": dated,
        # Named so the UI cannot accidentally merge them into one axis.
        "undated_bucket": undated,
        "counts": {"dated": len(dated), "undated": len(undated)},
        "note": "Undated events are shown separately and are never interpolated (§16).",
    }
