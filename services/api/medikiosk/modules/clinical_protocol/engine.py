"""The deterministic clinical protocol engine (CLAUDE.md §10).

    R(state) = {f ∈ F | D(f,state) AND f.required}
    Completeness(session) = |Answered ∩ R(state)| / |R(state)|
    NextField(session)    = argmin_{f ∈ R(state) \\ Answered} O(f)

``NextField`` is ``argmin`` over a total order. It is **never ML-ranked and never
LLM-chosen** [RED LINE §10]. Nothing in this module imports an AI client, makes a
network call, or touches a database — it is a pure function of (protocol, state),
which is what makes the Phase 1 Definition of Done (near-100% branch coverage
against in-memory fixtures) achievable at all.

The AMPLE fast path (§14) is expressed here as a *substitution of the required
set*, not as a reset:

1. ``Answered`` is never cleared.
2. ``R(state)`` becomes the fixed AMPLE set, layered on what is already answered.
3. Completeness is computed against that reduced set.
4. Skipped questions are attributable to the escalation, not to the patient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from medikiosk.modules.clinical_protocol import predicates
from medikiosk.modules.clinical_protocol.model import (
    Field,
    Protocol,
    ValueType,
)


class SkipReason(StrEnum):
    """Why a required field has no answer.

    §14.4: the physician's dashboard must never conflate "we didn't get to ask"
    with "the patient didn't know". These are distinct values, all the way to the
    database column and the dashboard label.
    """

    NOT_ANSWERED = "not_answered"
    NOT_ASKED_DUE_TO_EMERGENCY_ESCALATION = "not_asked_due_to_emergency_escalation"
    PATIENT_DECLINED = "patient_declined"
    PATIENT_UNSURE = "patient_unsure"


@dataclass(frozen=True, slots=True)
class AnswerRecord:
    """One accepted answer, as the engine sees it."""

    field_id: str
    value: Any
    confidence: float = 1.0
    confirmed: bool = False
    skip_reason: SkipReason | None = None

    @property
    def is_substantive(self) -> bool:
        """Does this record contribute to completeness?

        A declined or unsure answer *is* an answer — the patient was asked and
        responded. An escalation skip is not: nobody asked.
        """
        if self.skip_reason is None:
            return True
        return self.skip_reason in (SkipReason.PATIENT_DECLINED, SkipReason.PATIENT_UNSURE)


@dataclass(frozen=True, slots=True)
class SessionState:
    """The engine's complete view of an in-flight interview."""

    answers: Mapping[str, AnswerRecord] = field(default_factory=dict)
    fast_path_active: bool = False

    def answer_view(self) -> dict[str, Any]:
        """Predicate-facing view: field_id -> normalized value.

        Only substantive answers appear. A field skipped by escalation must not
        satisfy a downstream dependency, or the fast path would silently unlock
        questions nobody answered.
        """
        return {
            fid: rec.value
            for fid, rec in self.answers.items()
            if rec.is_substantive and rec.value is not None
        }

    def answered_ids(self) -> frozenset[str]:
        return frozenset(fid for fid, rec in self.answers.items() if rec.is_substantive)

    def with_answer(self, record: AnswerRecord) -> SessionState:
        merged = dict(self.answers)
        merged[record.field_id] = record
        return SessionState(answers=merged, fast_path_active=self.fast_path_active)

    def with_fast_path(self, *, active: bool = True) -> SessionState:
        # Answered is carried over untouched — §14.1.
        return SessionState(answers=dict(self.answers), fast_path_active=active)


# -----------------------------------------------------------------------------
# R(state) — the required set
# -----------------------------------------------------------------------------
def in_scope(protocol: Protocol, state: SessionState, f: Field) -> bool:
    """Is field ``f`` in scope given the current answers (predicate D)?"""
    return predicates.evaluate(f.depends_on, state.answer_view())


def required_fields(protocol: Protocol, state: SessionState) -> tuple[str, ...]:
    """R(state), in deterministic order O.

    On the fast path this returns the AMPLE set only (§14.2) — still filtered by
    dependency, so an AMPLE field gated on an unanswered question is not demanded.
    """
    if state.fast_path_active:
        candidates = protocol.ample_fields
    else:
        candidates = protocol.ordering

    view = state.answer_view()
    return tuple(
        fid
        for fid in candidates
        if _is_required(protocol.fields[fid], state)
        and predicates.evaluate(protocol.fields[fid].depends_on, view)
    )


def _is_required(f: Field, state: SessionState) -> bool:
    # On the fast path, membership of the AMPLE set is what makes a field
    # required — a routine-protocol `required` flag no longer governs.
    return f.ample if state.fast_path_active else f.required


def in_scope_fields(protocol: Protocol, state: SessionState) -> tuple[str, ...]:
    """Every field currently in scope, required or not, in order O.

    Optional-but-in-scope fields are what the kiosk offers when the patient taps
    "anything else you want to tell the doctor?" — they never gate completion.
    """
    view = state.answer_view()
    pool = protocol.ample_fields if state.fast_path_active else protocol.ordering
    return tuple(
        fid for fid in pool if predicates.evaluate(protocol.fields[fid].depends_on, view)
    )


def completeness(protocol: Protocol, state: SessionState) -> float:
    """|Answered ∩ R(state)| / |R(state)|, in [0, 1].

    An empty required set is complete by definition (1.0) rather than undefined —
    the alternative is a session that can never finish.
    """
    required = required_fields(protocol, state)
    if not required:
        return 1.0
    answered = state.answered_ids()
    satisfied = sum(1 for fid in required if fid in answered)
    return round(satisfied / len(required), 3)


def next_field(protocol: Protocol, state: SessionState) -> Field | None:
    """argmin over O of the unanswered required fields. ``None`` when complete.

    Deterministic by construction: ``protocol.ordering`` is a total order on
    (order, id), and this is a first-match scan over it.
    """
    answered = state.answered_ids()
    for fid in required_fields(protocol, state):
        if fid not in answered:
            return protocol.fields[fid]
    return None


def remaining_required(protocol: Protocol, state: SessionState) -> tuple[str, ...]:
    answered = state.answered_ids()
    return tuple(fid for fid in required_fields(protocol, state) if fid not in answered)


def is_complete(protocol: Protocol, state: SessionState) -> bool:
    return next_field(protocol, state) is None


def group_progress(protocol: Protocol, state: SessionState) -> tuple[dict[str, Any], ...]:
    """Per-group progress, for the kiosk's "where am I?" affordance.

    Low-literacy and elderly users need a concrete sense of position (§1), and a
    single global percentage does not provide it.
    """
    required = required_fields(protocol, state)
    answered = state.answered_ids()
    per_group: dict[str, dict[str, int]] = {}
    for fid in required:
        group = protocol.fields[fid].group
        bucket = per_group.setdefault(group, {"required": 0, "answered": 0})
        bucket["required"] += 1
        if fid in answered:
            bucket["answered"] += 1

    return tuple(
        {
            "group": group,
            "required": per_group[group]["required"],
            "answered": per_group[group]["answered"],
            "complete": per_group[group]["answered"] >= per_group[group]["required"],
        }
        for group in protocol.groups
        if group in per_group
    )


# -----------------------------------------------------------------------------
# Confidence gate (§10)
#   κ(v) ≥ τ_high(f)        → accept
#   τ_low ≤ κ(v) < τ_high   → confirm-back to respondent
#   κ(v) < τ_low(f)         → reject / re-prompt
# -----------------------------------------------------------------------------
class ConfidenceVerdict(StrEnum):
    ACCEPT = "accept"
    CONFIRM = "confirm"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Deployment-level defaults.

    [RED LINE §53] These are placeholders until calibrated on real pilot data.
    The field names say so, so no report can quote them as final.
    """

    tau_high_placeholder: float = 0.85
    tau_low_placeholder: float = 0.55

    def for_field(self, f: Field) -> tuple[float, float]:
        high = f.tau_high if f.tau_high is not None else self.tau_high_placeholder
        low = f.tau_low if f.tau_low is not None else self.tau_low_placeholder
        if low > high:
            # Inverted thresholds are a governed-content error. Fail CLOSED:
            # raise τ_high out of reach so nothing is ever silently auto-accepted
            # under a broken configuration — every answer goes to confirm-back
            # (or reject, below τ_low), which is the safe direction.
            return float("inf"), low
        return high, low


def gate_confidence(
    f: Field,
    confidence: float,
    thresholds: Thresholds,
) -> ConfidenceVerdict:
    high, low = thresholds.for_field(f)
    if confidence < low:
        return ConfidenceVerdict.REJECT
    if confidence < high:
        return ConfidenceVerdict.CONFIRM
    # A field marked confirm_back is confirmed even at high confidence: some
    # answers are too consequential to accept silently regardless of κ.
    return ConfidenceVerdict.CONFIRM if f.confirm_back else ConfidenceVerdict.ACCEPT


# -----------------------------------------------------------------------------
# Answer validation — structural, not clinical.
# -----------------------------------------------------------------------------
class AnswerValidationError(ValueError):
    def __init__(self, field_id: str, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.field_id = field_id
        self.reason_code = reason_code


_DURATION_UNITS = ("minutes", "hours", "days", "weeks", "months", "years")
_FREQUENCY_UNITS = ("per_day", "per_week", "per_month", "occasional", "constant")


def validate_and_normalize(f: Field, raw: Any) -> Any:
    """Coerce a submitted answer into its canonical, language-neutral form.

    Language never reaches here: a select answer is an option *code*, and the
    kiosk resolved the visible label from the localization layer before the
    patient tapped it. That is what keeps the engine language-neutral.
    """
    if raw is None:
        raise AnswerValidationError(f.id, "value_required", "answer is required")

    match f.value_type:
        case ValueType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.lower() in ("true", "false", "yes", "no"):
                return raw.lower() in ("true", "yes")
            raise AnswerValidationError(f.id, "expected_boolean", "expected yes or no")

        case ValueType.SINGLE_SELECT:
            code = _code_of(raw)
            if code not in f.option_values():
                raise AnswerValidationError(f.id, "unknown_option", "option is not offered")
            return {"code": code}

        case ValueType.MULTI_SELECT:
            codes = _codes_of(raw)
            if not codes:
                raise AnswerValidationError(f.id, "value_required", "select at least one")
            unknown = codes - f.option_values()
            if unknown:
                raise AnswerValidationError(f.id, "unknown_option", "option is not offered")
            exclusive = {o.value for o in f.options if o.exclusive}
            if codes & exclusive and len(codes) > 1:
                raise AnswerValidationError(
                    f.id, "exclusive_option_conflict", "that option cannot be combined"
                )
            return {"codes": sorted(codes)}

        case ValueType.SCALE:
            number = _number_of(raw, f.id)
            lo = f.validation.min if f.validation.min is not None else 0
            hi = f.validation.max if f.validation.max is not None else 10
            if not lo <= number <= hi:
                raise AnswerValidationError(f.id, "out_of_range", "value is out of range")
            return {"value": int(number)}

        case ValueType.NUMBER:
            number = _number_of(raw, f.id)
            if f.validation.min is not None and number < f.validation.min:
                raise AnswerValidationError(f.id, "out_of_range", "value is too low")
            if f.validation.max is not None and number > f.validation.max:
                raise AnswerValidationError(f.id, "out_of_range", "value is too high")
            payload: dict[str, Any] = {"value": number}
            unit = _unit_of(raw)
            if unit:
                _assert_unit(f, unit, f.validation.units)
                payload["unit"] = unit
            return payload

        case ValueType.DURATION:
            if not isinstance(raw, Mapping):
                raise AnswerValidationError(f.id, "expected_duration", "expected a duration")
            number = _number_of(raw.get("value"), f.id)
            unit = str(raw.get("unit", "")).lower()
            _assert_unit(f, unit, f.validation.units or _DURATION_UNITS)
            if number <= 0:
                raise AnswerValidationError(f.id, "out_of_range", "duration must be positive")
            return {"value": int(number), "unit": unit}

        case ValueType.FREQUENCY:
            if isinstance(raw, Mapping):
                unit = str(raw.get("unit", "")).lower()
                _assert_unit(f, unit, f.validation.units or _FREQUENCY_UNITS)
                value = raw.get("value")
                if value is None:
                    return {"unit": unit}
                return {"value": int(_number_of(value, f.id)), "unit": unit}
            code = _code_of(raw)
            _assert_unit(f, code, f.validation.units or _FREQUENCY_UNITS)
            return {"unit": code}

        case ValueType.BODY_REGION:
            codes = _codes_of(raw)
            if not codes:
                raise AnswerValidationError(f.id, "value_required", "select a body area")
            if f.options:
                unknown = codes - f.option_values()
                if unknown:
                    raise AnswerValidationError(f.id, "unknown_option", "area is not offered")
            return {"codes": sorted(codes)}

        case ValueType.DATE:
            text = str(raw).strip()
            if not _looks_like_iso_date(text):
                raise AnswerValidationError(f.id, "expected_date", "expected a date")
            return {"date": text}

        case ValueType.TEXT:
            text = str(raw).strip()
            if not text:
                raise AnswerValidationError(f.id, "value_required", "answer is required")
            limit = f.validation.max_length or 2000
            if len(text) > limit:
                raise AnswerValidationError(f.id, "too_long", "answer is too long")
            return {"text": text}

    raise AnswerValidationError(f.id, "unsupported_value_type", "unsupported value type")


def _code_of(raw: Any) -> str:
    if isinstance(raw, Mapping):
        for key in ("code", "value", "selected"):
            if key in raw:
                return str(raw[key])
    return str(raw)


def _codes_of(raw: Any) -> set[str]:
    if isinstance(raw, Mapping):
        for key in ("codes", "values", "selected"):
            if key in raw:
                return {str(v) for v in _as_list(raw[key])}
        if "code" in raw:
            return {str(raw["code"])}
    return {str(v) for v in _as_list(raw)}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _number_of(raw: Any, field_id: str) -> float:
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    if isinstance(raw, bool):
        raise AnswerValidationError(field_id, "expected_number", "expected a number")
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AnswerValidationError(field_id, "expected_number", "expected a number") from exc


def _unit_of(raw: Any) -> str | None:
    if isinstance(raw, Mapping) and raw.get("unit"):
        return str(raw["unit"]).lower()
    return None


def _assert_unit(f: Field, unit: str, allowed: tuple[str, ...]) -> None:
    if unit not in allowed:
        raise AnswerValidationError(f.id, "unknown_unit", "unit is not offered")


def _looks_like_iso_date(text: str) -> bool:
    parts = text.split("-")
    if len(parts) != 3:
        return False
    try:
        year, month, day = (int(p) for p in parts)
    except ValueError:
        return False
    return 1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31
