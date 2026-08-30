"""Clinical-safety regression suite (CLAUDE.md §52).

[RED LINE §52] This suite is a DEPLOYMENT GATE. A PR that breaks it does not
merge, regardless of who authored it or what else it fixes. It runs on every PR,
system-wide and non-skippable (§46, §61).

The corpus lives in ``golden_scenarios.json`` so that the Clinical Governance
Board can read and amend the scenarios without reading Python.

Two directions are checked for every scenario, because a safety suite that only
checks firing is half a suite:

* ``must_fire``     — a miss is a **false negative**, the dangerous direction.
* ``must_not_fire`` — a hit is a **false positive**, which floods the nurse queue
  until real criticals get ignored. ``"*"`` means "no rule at all may fire".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medikiosk.modules.triage import red_flag_engine

pytestmark = [pytest.mark.red_flag, pytest.mark.unit]

CORPUS_PATH = Path(__file__).parent / "golden_scenarios.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text("utf-8"))
SCENARIOS: list[dict[str, Any]] = CORPUS["scenarios"]


def _protocol_for(scenario: dict[str, Any], registry) -> Any:
    spec = scenario.get("protocol", CORPUS["protocol"])
    return registry.load(spec["family"], spec["version"])


@pytest.fixture(scope="module")
def ruleset(red_flag_registry):
    return red_flag_registry.load(CORPUS["ruleset"])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_golden_scenario(scenario, ruleset, protocol_registry):
    protocol = _protocol_for(scenario, protocol_registry)
    result = red_flag_engine.evaluate(ruleset, protocol, scenario["answers"])

    fired = {e.rule_id for e in result.fired}
    evaluated = {e.rule_id for e in result.evaluations}

    # --- false negatives -----------------------------------------------------
    expected_fire = set(scenario["must_fire"])
    unreachable = expected_fire - evaluated
    assert not unreachable, (
        f"{scenario['id']}: rules {sorted(unreachable)} were never evaluated for "
        f"protocol {protocol.key} — they are disarmed, not merely silent"
    )
    missed = expected_fire - fired
    assert not missed, (
        f"{scenario['id']}: FALSE NEGATIVE — expected {sorted(missed)} to fire.\n"
        f"  {scenario['description']}"
    )

    # --- false positives -----------------------------------------------------
    must_not = set(scenario["must_not_fire"])
    if "*" in must_not:
        assert not fired, (
            f"{scenario['id']}: FALSE POSITIVE — nothing should fire, got {sorted(fired)}.\n"
            f"  {scenario['description']}"
        )
    else:
        spurious = must_not & fired
        assert not spurious, (
            f"{scenario['id']}: FALSE POSITIVE — {sorted(spurious)} must not fire.\n"
            f"  {scenario['description']}"
        )

    # --- workflow consequences (§14) ----------------------------------------
    assert result.requires_fast_path is scenario["expect_fast_path"], (
        f"{scenario['id']}: fast-path expectation violated "
        f"(fired: {sorted(fired)})"
    )
    assert bool(result.alerts) is scenario["expect_alert"], (
        f"{scenario['id']}: alert expectation violated (fired: {sorted(fired)})"
    )


class TestSuiteIntegrity:
    """The gate must itself be hard to weaken."""

    def test_corpus_is_not_empty(self):
        assert len(SCENARIOS) >= 20, "the golden corpus has been thinned out"

    def test_scenario_ids_are_unique(self):
        ids = [s["id"] for s in SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_every_scenario_declares_both_directions(self):
        for scenario in SCENARIOS:
            assert "must_fire" in scenario, scenario["id"]
            assert "must_not_fire" in scenario, scenario["id"]
            assert "expect_fast_path" in scenario, scenario["id"]
            assert "expect_alert" in scenario, scenario["id"]
            assert scenario.get("description"), scenario["id"]

    def test_every_scenario_references_real_rule_ids(self, ruleset):
        known = {r.id for r in ruleset.rules}
        for scenario in SCENARIOS:
            referenced = set(scenario["must_fire"]) | (
                set(scenario["must_not_fire"]) - {"*"}
            )
            unknown = referenced - known
            assert not unknown, f"{scenario['id']} references unknown rules: {unknown}"

    def test_every_scenario_uses_real_field_ids(self, protocol_registry):
        for scenario in SCENARIOS:
            protocol = _protocol_for(scenario, protocol_registry)
            unknown = set(scenario["answers"]) - set(protocol.fields)
            assert not unknown, f"{scenario['id']} answers unknown fields: {unknown}"

    def test_corpus_covers_edge_cases_required_by_the_spec(self):
        """§52 names three edge-case shapes explicitly."""
        text = json.dumps(SCENARIOS).lower()
        assert "incomplete history" in text
        assert "conflicting answers" in text
        assert "borderline" in text

    def test_every_active_rule_is_exercised_by_the_corpus(self, ruleset):
        """A rule nobody tests is a rule nobody can trust.

        Every active rule must appear in at least one scenario's ``must_fire``,
        so the corpus proves the rule can fire at all.
        """
        proven: set[str] = set()
        for scenario in SCENARIOS:
            proven.update(scenario["must_fire"])
        untested = {r.id for r in ruleset.active_rules()} - proven
        assert not untested, f"active rules never proven to fire: {sorted(untested)}"

    def test_every_severity_level_is_represented(self, ruleset):
        severities = {r.severity for r in ruleset.active_rules()}
        assert severities == {"moderate", "high", "critical"}


class TestEngineDeterminism:
    def test_repeated_evaluation_is_identical(self, ruleset, protocol_registry):
        """A safety engine that is not deterministic is not testable."""
        scenario = next(s for s in SCENARIOS if s["id"] == "gold.acs.textbook")
        protocol = _protocol_for(scenario, protocol_registry)
        signatures = set()
        for _ in range(25):
            result = red_flag_engine.evaluate(ruleset, protocol, scenario["answers"])
            signatures.add(tuple(sorted((e.rule_id, e.fired) for e in result.evaluations)))
        assert len(signatures) == 1

    def test_every_rule_is_logged_whether_or_not_it_fires(self, ruleset, protocol_registry):
        """§14: RuleEvaluated --> Logged (always, fired or not).

        Without the negatives, false-positive and false-negative rates cannot be
        measured, and the safety claim becomes unfalsifiable.
        """
        protocol = protocol_registry.load("general_medicine", "v1")
        result = red_flag_engine.evaluate(ruleset, protocol, {})
        applicable = {r.id for r in ruleset.rules_for(protocol)}
        assert {e.rule_id for e in result.evaluations} == applicable
        assert all(e.fired is False for e in result.evaluations)

    def test_evaluated_state_snapshot_is_minimal_and_reproducible(
        self, ruleset, protocol_registry
    ):
        """The persisted snapshot must contain exactly the fields the rule read."""
        scenario = next(s for s in SCENARIOS if s["id"] == "gold.acs.textbook")
        protocol = _protocol_for(scenario, protocol_registry)
        result = red_flag_engine.evaluate(ruleset, protocol, scenario["answers"])
        by_id = {e.rule_id: e for e in result.evaluations}
        rule = next(r for r in ruleset.rules if r.id == "rf.acs.crushing_central_chest_pain")
        snapshot = by_id[rule.id].evaluated_state
        assert set(snapshot) == set(rule.input_fields) & set(scenario["answers"])

    def test_engine_module_is_free_of_ai_and_io(self):
        """[RED LINE §10] AI never decides whether a red flag fires."""
        import inspect

        source = inspect.getsource(red_flag_engine)
        for banned in ("httpx", "asyncpg", "openai", "anthropic", "requests",
                       "ai_gateway", "sklearn", "torch"):
            assert f"import {banned}" not in source, banned

    def test_malformed_rule_fails_safe_without_taking_down_the_interview(
        self, protocol_registry, tmp_path
    ):
        """A broken rule must not crash the session, and must not be silently
        treated as a pass either — it records a non-fire and logs loudly."""
        from medikiosk.modules.triage.red_flag_engine import (
            RedFlagRule,
            RuleSet,
            Severity,
        )

        protocol = protocol_registry.load("general_medicine", "v1")
        broken = RedFlagRule(
            id="rf.broken",
            name="broken",
            severity=Severity.CRITICAL,
            sla_seconds=60,
            category="test",
            predicate={"op": "not_a_real_operator", "field": "gm.hpi.severity", "value": 1},
            staff_rationale="x" * 40,
            input_fields=("gm.hpi.severity",),
        )
        ruleset = RuleSet(version="broken", content_checksum="x", rules=(broken,))
        result = red_flag_engine.evaluate(ruleset, protocol, {"gm.hpi.severity": {"value": 10}})
        assert len(result.evaluations) == 1
        assert result.evaluations[0].fired is False
        assert result.requires_fast_path is False


class TestSeveritySemantics:
    def test_only_critical_triggers_the_fast_path(self):
        from medikiosk.modules.triage.red_flag_engine import Severity

        assert Severity.CRITICAL.triggers_fast_path is True
        assert Severity.HIGH.triggers_fast_path is False
        assert Severity.MODERATE.triggers_fast_path is False

    def test_high_and_critical_create_alerts_moderate_does_not(self):
        from medikiosk.modules.triage.red_flag_engine import Severity

        assert Severity.CRITICAL.creates_alert is True
        assert Severity.HIGH.creates_alert is True
        assert Severity.MODERATE.creates_alert is False

    def test_highest_severity_reports_the_worst_fired(self, ruleset, protocol_registry):
        scenario = next(s for s in SCENARIOS if s["id"] == "gold.acs.textbook")
        protocol = _protocol_for(scenario, protocol_registry)
        result = red_flag_engine.evaluate(ruleset, protocol, scenario["answers"])
        assert result.highest_severity == "critical"

    def test_highest_severity_is_none_when_nothing_fires(self, ruleset, protocol_registry):
        protocol = protocol_registry.load("general_medicine", "v1")
        result = red_flag_engine.evaluate(ruleset, protocol, {})
        assert result.highest_severity is None
