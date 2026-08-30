"""Red-Flag Engine — fully deterministic (CLAUDE.md §14).

    [*] --> RuleEvaluated (every answer, same transaction, forward-chaining)
    RuleEvaluated --> Logged (always, fired or not)
    RuleEvaluated --> AlertCreated (rule fires, severity ∈ {high, critical})

[RED LINE §10] AI never decides whether a red flag fires. This module has no AI
client and no network dependency: it is a pure function from (ruleset, answers) to
(evaluations, fired rules), which is exactly what makes the golden-file regression
suite of §52 a meaningful deployment gate.

Every evaluation is returned — fired *and* not fired — because §14 requires
false-positive and false-negative rates to be measurable. Discarding the negatives
would make the safety claim unfalsifiable.

Rule predicates use the same governed expression language as protocol
dependencies (:mod:`medikiosk.modules.clinical_protocol.predicates`). One language
means a clinician on the Governance Board reads one syntax, and a typo in either
place fails at load rather than in front of a patient.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from medikiosk.modules.clinical_protocol import predicates
from medikiosk.modules.clinical_protocol.model import (
    Protocol,
    ProtocolContentError,
    referenced_fields,
)
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


class Severity(StrEnum):
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def creates_alert(self) -> bool:
        """§14: an alert is created for high and critical.

        Moderate fires and is logged, and is surfaced to the physician as a
        review flag, but does not page a nurse — otherwise the queue becomes
        noise and real criticals get lost.
        """
        return self in (Severity.HIGH, Severity.CRITICAL)

    @property
    def triggers_fast_path(self) -> bool:
        """Only a critical rule collapses the interview to AMPLE (§14)."""
        return self is Severity.CRITICAL


@dataclass(frozen=True, slots=True)
class RedFlagRule:
    id: str
    name: str
    severity: Severity
    sla_seconds: int
    category: str
    predicate: Mapping[str, Any]
    # Clinician-facing rationale. Authored and reviewed as clinical content by the
    # Governance Board, which is why it lives with the rule and not in i18n.
    # It is NEVER shown on the kiosk (§14 patient-facing behaviour).
    staff_rationale: str
    # Fields the rule reads. Validated against the protocol at load time so that
    # deleting a protocol field cannot silently disarm a safety rule.
    input_fields: tuple[str, ...]
    active: bool = True
    # Clinical provenance of the rule, for the governance queue.
    reference: str | None = None
    # Protocol families the rule applies to. Empty means "wherever its input
    # fields exist" — which, thanks to protocol composition (§12), is how a
    # General Medicine rule keeps protecting an AYUSH session that inherited the
    # same gm.* fields.
    families: tuple[str, ...] = ()

    def applies_to(self, family: str) -> bool:
        return not self.families or family in self.families


@dataclass(frozen=True, slots=True)
class RuleSet:
    version: str
    content_checksum: str
    rules: tuple[RedFlagRule, ...]

    def active_rules(self) -> tuple[RedFlagRule, ...]:
        return tuple(r for r in self.rules if r.active)

    def rules_for(self, protocol: Protocol) -> tuple[RedFlagRule, ...]:
        """Active rules whose inputs actually exist in this protocol.

        Filtering by field existence rather than by a hand-maintained family list
        is what prevents a rule from silently never firing: if its inputs are
        present it runs, and if they are absent the governance validation reports
        it rather than the engine quietly skipping it.
        """
        available = set(protocol.fields)
        return tuple(
            r
            for r in self.active_rules()
            if r.applies_to(protocol.family) and set(r.input_fields).issubset(available)
        )


@dataclass(frozen=True, slots=True)
class Evaluation:
    rule_id: str
    fired: bool
    severity: Severity
    rule_name: str
    sla_seconds: int
    staff_rationale: str
    # The exact subset of answers the rule read, for reproducibility (§14).
    evaluated_state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EngineResult:
    evaluations: tuple[Evaluation, ...]
    ruleset_version: str

    @property
    def fired(self) -> tuple[Evaluation, ...]:
        return tuple(e for e in self.evaluations if e.fired)

    @property
    def alerts(self) -> tuple[Evaluation, ...]:
        return tuple(e for e in self.fired if e.severity.creates_alert)

    @property
    def requires_fast_path(self) -> bool:
        return any(e.severity.triggers_fast_path for e in self.fired)

    @property
    def highest_severity(self) -> Severity | None:
        order = {Severity.MODERATE: 0, Severity.HIGH: 1, Severity.CRITICAL: 2}
        fired = self.fired
        if not fired:
            return None
        return max((e.severity for e in fired), key=lambda s: order[s])


class RedFlagRegistry:
    def __init__(self, content_root: Path) -> None:
        self._root = content_root / "redflag"
        self._cache: dict[str, RuleSet] = {}

    def load(self, version: str) -> RuleSet:
        cached = self._cache.get(version)
        if cached is not None:
            return cached

        path = self._root / f"{version}.json"
        if not path.is_file():
            raise ProtocolContentError(f"red-flag ruleset not found: {version}")

        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        if str(document.get("version")) != version:
            raise ProtocolContentError(
                f"ruleset declares version {document.get('version')!r}, expected {version!r}"
            )

        rules: list[RedFlagRule] = []
        seen: set[str] = set()
        for entry in document.get("rules", []):
            rule = _parse_rule(entry)
            if rule.id in seen:
                raise ProtocolContentError(f"duplicate red-flag rule id: {rule.id}")
            seen.add(rule.id)
            rules.append(rule)

        if not rules:
            raise ProtocolContentError(f"ruleset {version} contains no rules")

        ruleset = RuleSet(version=version, content_checksum=checksum, rules=tuple(rules))
        self._cache[version] = ruleset
        log.info(
            "red_flag_ruleset_loaded",
            component="triage",
            ruleset_version=version,
            count=len(rules),
        )
        return ruleset

    def validate_against(
        self, ruleset: RuleSet, protocols: tuple[Protocol, ...]
    ) -> tuple[str, ...]:
        """Return rule ids that are disarmed or mis-declared.

        A rule is disarmed if no loaded protocol defines all of its input fields:
        it can then never fire, which is a silent safety regression. It is
        mis-declared if ``input_fields`` disagrees with what its predicate
        actually reads, which would make the persisted ``evaluated_state``
        snapshot incomplete and the evaluation irreproducible (§14).

        The CI clinical-safety gate treats a non-empty result as a failure (§52).
        """
        broken: list[str] = []
        all_fields = [set(p.fields) for p in protocols]
        for rule in ruleset.rules:
            declared = set(rule.input_fields)
            actual = set(referenced_fields(rule.predicate))
            if declared != actual:
                broken.append(rule.id)
                continue
            if not any(actual.issubset(fields) for fields in all_fields):
                broken.append(rule.id)
        return tuple(broken)


def _parse_rule(entry: dict[str, Any]) -> RedFlagRule:
    for key in ("id", "name", "severity", "sla_seconds", "category", "predicate",
                "staff_rationale"):
        if key not in entry:
            raise ProtocolContentError(f"red-flag rule missing required key {key!r}")

    try:
        severity = Severity(entry["severity"])
    except ValueError as exc:
        raise ProtocolContentError(f"rule {entry['id']}: {exc}") from exc

    predicate = entry["predicate"]
    predicates.validate_predicate(predicate)
    referenced = referenced_fields(predicate)
    if not referenced:
        raise ProtocolContentError(
            f"rule {entry['id']} reads no fields and would fire unconditionally"
        )

    sla = int(entry["sla_seconds"])
    if sla <= 0:
        raise ProtocolContentError(f"rule {entry['id']} must declare a positive SLA")

    return RedFlagRule(
        id=str(entry["id"]),
        name=str(entry["name"]),
        severity=severity,
        sla_seconds=sla,
        category=str(entry["category"]),
        predicate=predicate,
        staff_rationale=str(entry["staff_rationale"]),
        input_fields=tuple(sorted(entry.get("input_fields", sorted(referenced)))),
        active=bool(entry.get("active", True)),
        reference=entry.get("reference"),
        families=tuple(entry.get("families", ()) or ()),
    )


# -----------------------------------------------------------------------------
# Evaluation
# -----------------------------------------------------------------------------
def evaluate(
    ruleset: RuleSet,
    protocol: Protocol,
    answers: Mapping[str, Any],
) -> EngineResult:
    """Evaluate every applicable rule against the current answer view.

    Forward-chaining is achieved by re-evaluating the whole active ruleset after
    every answer: rules are independent predicates over accumulated state, so a
    full pass is both simpler and strictly safer than incremental invalidation —
    there is no cache to go stale and no ordering dependency between rules.

    The full pass is affordable inside the §54 budget (<50 ms p95) because the
    ruleset is small and each predicate is a handful of dictionary lookups.
    """
    evaluations: list[Evaluation] = []
    for rule in ruleset.rules_for(protocol):
        # Snapshot only the fields this rule reads: the smallest reproducible
        # state, and nothing extra written into red_flag_evaluation.
        state = {fid: answers[fid] for fid in rule.input_fields if fid in answers}
        try:
            fired = predicates.evaluate(rule.predicate, answers)
        except ProtocolContentError:
            # A malformed rule must not take the interview down, and must not be
            # silently treated as "did not fire" either. Fail safe: log loudly
            # and record a non-fire so the governance queue surfaces it.
            log.error(
                "red_flag_rule_malformed",
                component="triage",
                rule_id=rule.id,
                ruleset_version=ruleset.version,
                fired=False,
            )
            fired = False
        evaluations.append(
            Evaluation(
                rule_id=rule.id,
                fired=fired,
                severity=rule.severity,
                rule_name=rule.name,
                sla_seconds=rule.sla_seconds,
                staff_rationale=rule.staff_rationale,
                evaluated_state=state,
            )
        )
    return EngineResult(evaluations=tuple(evaluations), ruleset_version=ruleset.version)
