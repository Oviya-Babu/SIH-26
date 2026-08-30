"""Protocol domain model (CLAUDE.md §10).

    Protocol = (C, F, D, R, O)
      C = clinical concepts   F = question-field nodes   D = dependency predicates
      R(state) = required set  O = deterministic ordering

**This model carries no human-readable text.** A field has a stable ``id`` and a
``concept_code``; every string a patient ever sees or hears is resolved later, by
the localization layer, from that id. Two consequences that matter:

* the engine is language-neutral — adding Tamil cannot change branching;
* a protocol content change is reviewable as clinical logic, separately from a
  translation change (§59 governance).

There is exactly one engine. General Medicine and Ayurveda are two versioned
*data configurations* of this same code — not two systems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class ValueType(StrEnum):
    """The shape of an answer. Drives validation and normalisation, not wording."""

    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    SCALE = "scale"            # bounded integer, e.g. 0-10 severity
    DURATION = "duration"      # {value: int, unit: 'hours'|'days'|'weeks'|'months'|'years'}
    BODY_REGION = "body_region"
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    FREQUENCY = "frequency"


class Widget(StrEnum):
    """Presentation hint for the kiosk. A hint only — never clinical logic.

    Chosen for a touchscreen used by first-time, elderly and low-literacy
    patients (§1, §8): every widget here is large-target and icon-capable.
    """

    YES_NO = "yes_no"
    CHOICE_GRID = "choice_grid"
    MULTI_CHOICE_GRID = "multi_choice_grid"
    BODY_MAP = "body_map"
    SEVERITY_FACES = "severity_faces"
    DURATION_PICKER = "duration_picker"
    FREQUENCY_PICKER = "frequency_picker"
    NUMBER_PAD = "number_pad"
    DATE_PICKER = "date_picker"
    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"


@dataclass(frozen=True, slots=True)
class Concept:
    """A clinical concept (C). The stable clinical meaning behind fields."""

    code: str
    category: str
    # Free-text ASR/NLU slot-filling targets this concept, not a field label.
    nlu_slot: str | None = None


@dataclass(frozen=True, slots=True)
class Option:
    """A selectable answer value.

    ``value`` is the stable, language-neutral code that reaches clinical_fact.
    ``icon`` is a semantic icon name the kiosk maps to an asset — icons carry the
    meaning for a patient who cannot read the label (§1).
    """

    value: str
    icon: str | None = None
    # Marks options that must remain reachable even in a reduced/fast-path view.
    critical: bool = False
    # Mutually exclusive with every other option (e.g. "none of these").
    exclusive: bool = False


@dataclass(frozen=True, slots=True)
class Validation:
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    pattern: str | None = None
    # Units the kiosk may offer for a DURATION/NUMBER answer.
    units: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Field:
    """A question-field node (F)."""

    id: str
    concept_code: str
    category: str
    group: str
    order: int
    value_type: ValueType
    widget: Widget
    required: bool = False
    options: tuple[Option, ...] = ()
    # Dependency predicate (D). ``None`` means unconditionally in scope.
    depends_on: Mapping[str, Any] | None = None
    validation: Validation = field(default_factory=Validation)
    # Confirm the interpreted value back to the respondent before accepting.
    confirm_back: bool = False
    # Per-field confidence thresholds. [RED LINE §53] placeholders until
    # calibrated on pilot data; a None falls back to the deployment default.
    tau_high: float | None = None
    tau_low: float | None = None
    # Member of the AMPLE fast-path set used only on red-flag escalation (§11, §14).
    ample: bool = False
    # Fields whose answer the red-flag engine reads. Documented here so a content
    # change that removes a field a rule depends on is caught by the CI gate.
    red_flag_input: bool = False
    # Answering this may make other fields required; used for ordering stability.
    unlocks: tuple[str, ...] = ()

    def option_values(self) -> frozenset[str]:
        return frozenset(o.value for o in self.options)


@dataclass(frozen=True, slots=True)
class Protocol:
    """A governed, versioned protocol instance."""

    family: str
    version: str
    content_checksum: str
    concepts: Mapping[str, Concept]
    fields: Mapping[str, Field]
    # Deterministic ordering (O), precomputed: (order, id).
    ordering: tuple[str, ...]
    groups: tuple[str, ...]
    # Field ids constituting the AMPLE fast-path required set (§14).
    ample_fields: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.family}:{self.version}"

    def field_or_raise(self, field_id: str) -> Field:
        try:
            return self.fields[field_id]
        except KeyError as exc:
            raise UnknownFieldError(field_id) from exc

    def concept_or_raise(self, code: str) -> Concept:
        try:
            return self.concepts[code]
        except KeyError as exc:
            raise ProtocolContentError(f"unknown concept: {code}") from exc


class ProtocolContentError(ValueError):
    """Governed content is internally inconsistent — refuse to load it."""


class UnknownFieldError(KeyError):
    def __init__(self, field_id: str) -> None:
        super().__init__(field_id)
        self.field_id = field_id


def build_protocol(
    *,
    family: str,
    version: str,
    content_checksum: str,
    concepts: list[Concept],
    fields: list[Field],
) -> Protocol:
    """Assemble and *validate* a protocol.

    Validation is not optional politeness: a dangling ``depends_on`` reference or
    a duplicate order would make ``NextField`` non-deterministic, and §10 requires
    it to be deterministic.
    """
    concept_map: dict[str, Concept] = {}
    for concept in concepts:
        if concept.code in concept_map:
            raise ProtocolContentError(f"duplicate concept code: {concept.code}")
        concept_map[concept.code] = concept

    field_map: dict[str, Field] = {}
    for f in fields:
        if f.id in field_map:
            raise ProtocolContentError(f"duplicate field id: {f.id}")
        if f.concept_code not in concept_map:
            raise ProtocolContentError(
                f"field {f.id} references unknown concept {f.concept_code}"
            )
        if f.value_type in (ValueType.SINGLE_SELECT, ValueType.MULTI_SELECT) and not f.options:
            raise ProtocolContentError(f"field {f.id} is a select but has no options")
        if f.value_type == ValueType.SCALE and (
            f.validation.min is None or f.validation.max is None
        ):
            raise ProtocolContentError(f"scale field {f.id} must declare min and max")
        seen_values: set[str] = set()
        for opt in f.options:
            if opt.value in seen_values:
                raise ProtocolContentError(f"duplicate option {opt.value} on field {f.id}")
            seen_values.add(opt.value)
        field_map[f.id] = f

    # Dependency references must resolve, and must not be self-referential.
    for f in field_map.values():
        for referenced in referenced_fields(f.depends_on):
            if referenced not in field_map:
                raise ProtocolContentError(
                    f"field {f.id} depends on unknown field {referenced}"
                )
            if referenced == f.id:
                raise ProtocolContentError(f"field {f.id} depends on itself")

    _assert_acyclic(field_map)

    ordering = tuple(sorted(field_map, key=lambda fid: (field_map[fid].order, fid)))
    groups: list[str] = []
    for fid in ordering:
        group = field_map[fid].group
        if group not in groups:
            groups.append(group)

    ample = tuple(fid for fid in ordering if field_map[fid].ample)
    if not ample:
        raise ProtocolContentError(
            f"protocol {family}:{version} defines no AMPLE fast-path fields "
            "(CLAUDE.md §14 requires a fast path)"
        )

    return Protocol(
        family=family,
        version=version,
        content_checksum=content_checksum,
        concepts=MappingProxyType(concept_map),
        fields=MappingProxyType(field_map),
        ordering=ordering,
        groups=tuple(groups),
        ample_fields=ample,
    )


def referenced_fields(predicate: Mapping[str, Any] | None) -> frozenset[str]:
    """Every field id a predicate reads. Used for validation and invalidation."""
    if not predicate:
        return frozenset()
    found: set[str] = set()
    _collect_fields(predicate, found)
    return frozenset(found)


def _collect_fields(node: Any, acc: set[str]) -> None:
    if isinstance(node, Mapping):
        if isinstance(node.get("field"), str):
            acc.add(node["field"])
        for value in node.values():
            _collect_fields(value, acc)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_fields(item, acc)


def _assert_acyclic(field_map: Mapping[str, Field]) -> None:
    """A dependency cycle would let the engine stall with no next question."""
    state: dict[str, int] = {}

    def visit(fid: str, path: tuple[str, ...]) -> None:
        marker = state.get(fid, 0)
        if marker == 1:
            cycle = " -> ".join((*path, fid))
            raise ProtocolContentError(f"dependency cycle: {cycle}")
        if marker == 2:
            return
        state[fid] = 1
        for dep in sorted(referenced_fields(field_map[fid].depends_on)):
            visit(dep, (*path, fid))
        state[fid] = 2

    for fid in sorted(field_map):
        visit(fid, ())
