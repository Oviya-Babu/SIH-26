"""The deterministic protocol engine (CLAUDE.md §10, §14, Phase 1 DoD).

What these tests actually protect:

* ``NextField`` is ``argmin`` over a total order — deterministic, never ranked.
* Completeness is computed against ``R(state)``, which shrinks and grows with
  dependencies rather than being a fixed denominator.
* The AMPLE fast path *substitutes* the required set and never clears answers,
  and an escalation-skipped question never counts as answered and never unlocks
  a dependency (§14.1–14.4).
"""

from __future__ import annotations

import pytest

from medikiosk.modules.clinical_protocol import engine
from medikiosk.modules.clinical_protocol.engine import (
    AnswerRecord,
    AnswerValidationError,
    ConfidenceVerdict,
    SessionState,
    SkipReason,
    Thresholds,
)
from medikiosk.modules.clinical_protocol.model import (
    Concept,
    Field,
    Option,
    ProtocolContentError,
    Validation,
    ValueType,
    Widget,
    build_protocol,
)

pytestmark = pytest.mark.unit


def answered(**pairs) -> SessionState:
    return SessionState(
        answers={fid: AnswerRecord(field_id=fid, value=value) for fid, value in pairs.items()}
    )


class TestNextFieldDeterminism:
    def test_first_question_is_lowest_ordered_required_field(self, toy_protocol):
        nxt = engine.next_field(toy_protocol, SessionState())
        assert nxt is not None
        assert nxt.id == "gm.toy.root"

    def test_repeated_calls_are_identical(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "pain"}})
        results = {engine.next_field(toy_protocol, state).id for _ in range(50)}
        assert results == {"gm.toy.branch"}

    def test_ordering_breaks_ties_by_field_id(self):
        """Two fields with the same order must still have a total order."""
        concepts = [Concept(code="c", category="symptom")]
        fields = [
            Field(id=f"gm.tie.{name}", concept_code="c", category="symptom", group="g",
                  order=10, required=True, value_type=ValueType.BOOLEAN,
                  widget=Widget.YES_NO, ample=True)
            for name in ("bravo", "alpha", "charlie")
        ]
        protocol = build_protocol(family="general_medicine", version="tie",
                                 content_checksum="x", concepts=concepts, fields=fields)
        assert protocol.ordering == ("gm.tie.alpha", "gm.tie.bravo", "gm.tie.charlie")
        assert engine.next_field(protocol, SessionState()).id == "gm.tie.alpha"

    def test_none_when_all_required_answered(self, toy_protocol):
        state = answered(**{
            "gm.toy.root": {"code": "fever"},   # branch is out of scope
            "gm.toy.scale": {"value": 4},
        })
        assert engine.next_field(toy_protocol, state) is None
        assert engine.is_complete(toy_protocol, state) is True

    def test_engine_module_has_no_ai_or_io_imports(self):
        """[RED LINE §10] NextField is never ML-ranked or LLM-chosen.

        Asserted structurally: the engine module must not import an AI client, a
        database driver, or an HTTP library. A future refactor that wires an LLM
        into question selection fails here.
        """
        import inspect

        source = inspect.getsource(engine)
        forbidden = ("httpx", "asyncpg", "openai", "anthropic", "requests",
                     "ai_gateway", "sklearn", "torch", "redis")
        offenders = [name for name in forbidden if f"import {name}" in source]
        assert offenders == [], f"engine must stay pure; found imports: {offenders}"


class TestDependencyScoping:
    def test_dependent_field_absent_until_dependency_satisfied(self, toy_protocol):
        assert "gm.toy.branch" not in engine.required_fields(toy_protocol, SessionState())
        state = answered(**{"gm.toy.root": {"code": "pain"}})
        assert "gm.toy.branch" in engine.required_fields(toy_protocol, state)

    def test_dependency_not_satisfied_keeps_field_out(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "fever"}})
        assert "gm.toy.branch" not in engine.required_fields(toy_protocol, state)

    def test_optional_field_never_gates_completion(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "fever"}, "gm.toy.scale": {"value": 1}})
        assert engine.is_complete(toy_protocol, state)
        assert "gm.toy.optional" in engine.in_scope_fields(toy_protocol, state)
        assert "gm.toy.optional" not in engine.required_fields(toy_protocol, state)


class TestCompleteness:
    def test_zero_when_nothing_answered(self, toy_protocol):
        assert engine.completeness(toy_protocol, SessionState()) == 0.0

    def test_denominator_grows_when_a_branch_opens(self, toy_protocol):
        # 'fever' closes the branch: required = {root, scale}, 1 of 2 answered.
        closed = answered(**{"gm.toy.root": {"code": "fever"}})
        assert engine.completeness(toy_protocol, closed) == 0.5
        # 'pain' opens it: required = {root, branch, scale}, 1 of 3.
        opened = answered(**{"gm.toy.root": {"code": "pain"}})
        assert engine.completeness(toy_protocol, opened) == pytest.approx(0.333, abs=0.001)

    def test_one_when_complete(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "fever"}, "gm.toy.scale": {"value": 0}})
        assert engine.completeness(toy_protocol, state) == 1.0

    def test_empty_required_set_is_complete_not_undefined(self):
        concepts = [Concept(code="c", category="symptom")]
        fields = [
            Field(id="gm.opt.only", concept_code="c", category="symptom", group="g", order=1,
                  required=False, ample=True, value_type=ValueType.BOOLEAN,
                  widget=Widget.YES_NO),
        ]
        protocol = build_protocol(family="general_medicine", version="opt",
                                  content_checksum="x", concepts=concepts, fields=fields)
        assert engine.completeness(protocol, SessionState()) == 1.0

    def test_group_progress_reports_per_section(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "pain"}})
        progress = engine.group_progress(toy_protocol, state)
        by_group = {row["group"]: row for row in progress}
        assert by_group["g1"] == {"group": "g1", "required": 2, "answered": 1, "complete": False}
        assert by_group["g2"]["complete"] is False


class TestAmpleFastPath:
    def test_fast_path_never_clears_answers(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "pain"}}).with_fast_path()
        assert state.answers["gm.toy.root"].value == {"code": "pain"}
        assert state.fast_path_active is True

    def test_fast_path_substitutes_the_required_set(self, toy_protocol):
        routine = answered(**{"gm.toy.root": {"code": "pain"}})
        assert set(engine.required_fields(toy_protocol, routine)) == {
            "gm.toy.root", "gm.toy.branch", "gm.toy.scale"
        }
        fast = routine.with_fast_path()
        assert engine.required_fields(toy_protocol, fast) == ("gm.toy.ample",)

    def test_fast_path_reaches_completion_in_a_handful_of_questions(self, toy_protocol):
        state = answered(**{"gm.toy.root": {"code": "pain"}}).with_fast_path()
        assert engine.completeness(toy_protocol, state) == 0.0
        state = state.with_answer(AnswerRecord(field_id="gm.toy.ample", value=True))
        assert engine.completeness(toy_protocol, state) == 1.0
        assert engine.next_field(toy_protocol, state) is None

    def test_ample_field_is_unreachable_in_the_routine_interview(self, toy_protocol):
        """§11: AMPLE is used ONLY in the red-flag fast path."""
        state = SessionState()
        assert "gm.toy.ample" not in engine.required_fields(toy_protocol, state)
        # Even fully answered, the routine path never demands it.
        state = answered(**{"gm.toy.root": {"code": "fever"}, "gm.toy.scale": {"value": 2}})
        assert engine.is_complete(toy_protocol, state)
        assert "gm.toy.ample" not in engine.required_fields(toy_protocol, state)

    def test_protocol_without_ample_fields_is_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        fields = [
            Field(id="gm.noample.a", concept_code="c", category="symptom", group="g", order=1,
                  required=True, value_type=ValueType.BOOLEAN, widget=Widget.YES_NO),
        ]
        with pytest.raises(ProtocolContentError, match="no AMPLE fast-path fields"):
            build_protocol(family="general_medicine", version="noample",
                           content_checksum="x", concepts=concepts, fields=fields)


class TestSkipReasonSemantics:
    def test_escalation_skip_is_not_an_answer(self, toy_protocol):
        """§14.4: 'we did not get to ask' must never look like 'answered'."""
        state = SessionState(answers={
            "gm.toy.root": AnswerRecord(
                field_id="gm.toy.root",
                value=None,
                skip_reason=SkipReason.NOT_ASKED_DUE_TO_EMERGENCY_ESCALATION,
            )
        })
        assert state.answered_ids() == frozenset()
        assert engine.completeness(toy_protocol, state) == 0.0

    def test_escalation_skip_does_not_unlock_a_dependency(self, toy_protocol):
        state = SessionState(answers={
            "gm.toy.root": AnswerRecord(
                field_id="gm.toy.root",
                value={"code": "pain"},
                skip_reason=SkipReason.NOT_ASKED_DUE_TO_EMERGENCY_ESCALATION,
            )
        })
        assert "gm.toy.branch" not in engine.required_fields(toy_protocol, state)

    @pytest.mark.parametrize(
        "reason", [SkipReason.PATIENT_DECLINED, SkipReason.PATIENT_UNSURE]
    )
    def test_patient_declined_or_unsure_counts_as_answered(self, toy_protocol, reason):
        """The patient WAS asked and did respond; that is a substantive answer."""
        state = SessionState(answers={
            "gm.toy.root": AnswerRecord(field_id="gm.toy.root", value=None, skip_reason=reason)
        })
        assert "gm.toy.root" in state.answered_ids()

    def test_not_answered_is_not_substantive(self):
        record = AnswerRecord(field_id="f", value=None, skip_reason=SkipReason.NOT_ANSWERED)
        assert record.is_substantive is False


class TestConfidenceGate:
    def test_accept_above_tau_high(self, toy_protocol, thresholds):
        f = toy_protocol.fields["gm.toy.root"]
        assert engine.gate_confidence(f, 0.95, thresholds) is ConfidenceVerdict.ACCEPT

    def test_confirm_between_thresholds(self, toy_protocol, thresholds):
        f = toy_protocol.fields["gm.toy.root"]
        assert engine.gate_confidence(f, 0.70, thresholds) is ConfidenceVerdict.CONFIRM

    def test_reject_below_tau_low(self, toy_protocol, thresholds):
        f = toy_protocol.fields["gm.toy.root"]
        assert engine.gate_confidence(f, 0.20, thresholds) is ConfidenceVerdict.REJECT

    def test_confirm_back_field_always_confirms(self, toy_protocol, thresholds):
        """Some answers are too consequential to accept silently at any κ."""
        f = toy_protocol.fields["gm.toy.scale"]
        assert f.confirm_back is True
        assert engine.gate_confidence(f, 1.0, thresholds) is ConfidenceVerdict.CONFIRM

    def test_per_field_thresholds_override_deployment_defaults(self):
        f = Field(id="gm.t.x", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO,
                  tau_high=0.99, tau_low=0.9)
        t = Thresholds()
        assert engine.gate_confidence(f, 0.95, t) is ConfidenceVerdict.CONFIRM
        assert engine.gate_confidence(f, 0.85, t) is ConfidenceVerdict.REJECT

    def test_inverted_thresholds_fail_closed_to_confirmation(self):
        """Bad governed content must not silently auto-accept."""
        f = Field(id="gm.t.y", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO,
                  tau_high=0.5, tau_low=0.9)
        assert engine.gate_confidence(f, 0.99, Thresholds()) is ConfidenceVerdict.CONFIRM

    def test_thresholds_are_named_as_placeholders(self):
        """[RED LINE §53] the defaults must not read as calibrated numbers."""
        names = set(Thresholds.__dataclass_fields__)
        assert names == {"tau_high_placeholder", "tau_low_placeholder"}


class TestAnswerNormalization:
    def test_boolean_accepts_yes_no_strings(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.branch"]
        assert engine.validate_and_normalize(f, "yes") is True
        assert engine.validate_and_normalize(f, "no") is False
        assert engine.validate_and_normalize(f, True) is True

    def test_boolean_rejects_garbage(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.branch"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "maybe")
        assert exc.value.reason_code == "expected_boolean"

    def test_single_select_rejects_unoffered_option(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.root"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "amputation")
        assert exc.value.reason_code == "unknown_option"

    def test_single_select_normalizes_to_code_envelope(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.root"]
        assert engine.validate_and_normalize(f, "pain") == {"code": "pain"}
        assert engine.validate_and_normalize(f, {"code": "pain"}) == {"code": "pain"}

    def test_scale_range_enforced(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.scale"]
        assert engine.validate_and_normalize(f, 0) == {"value": 0}
        assert engine.validate_and_normalize(f, 10) == {"value": 10}
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, 11)
        assert exc.value.reason_code == "out_of_range"

    def test_scale_requires_a_number(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.scale"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "very bad")
        assert exc.value.reason_code == "expected_number"

    def test_text_length_limit(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.optional"]
        assert engine.validate_and_normalize(f, "  chest hurts  ") == {"text": "chest hurts"}
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "x" * 51)
        assert exc.value.reason_code == "too_long"

    def test_empty_text_is_rejected(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.optional"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "   ")
        assert exc.value.reason_code == "value_required"

    def test_none_is_always_rejected(self, toy_protocol):
        f = toy_protocol.fields["gm.toy.root"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, None)
        assert exc.value.reason_code == "value_required"

    def test_multi_select_exclusive_conflict(self, general_medicine):
        f = general_medicine.fields["gm.hpi.associated_symptoms"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, ["breathlessness", "no_other_symptoms"])
        assert exc.value.reason_code == "exclusive_option_conflict"

    def test_multi_select_sorts_for_stable_storage(self, general_medicine):
        f = general_medicine.fields["gm.hpi.associated_symptoms"]
        result = engine.validate_and_normalize(f, ["nausea", "breathlessness"])
        assert result == {"codes": ["breathlessness", "nausea"]}

    def test_multi_select_requires_at_least_one(self, general_medicine):
        f = general_medicine.fields["gm.hpi.associated_symptoms"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, [])
        assert exc.value.reason_code == "value_required"

    def test_duration_requires_a_declared_unit(self, general_medicine):
        f = general_medicine.fields["gm.cc.duration_of_concern"]
        assert engine.validate_and_normalize(f, {"value": 3, "unit": "days"}) == {
            "value": 3, "unit": "days"
        }
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, {"value": 3, "unit": "fortnights"})
        assert exc.value.reason_code == "unknown_unit"

    def test_duration_rejects_non_positive(self, general_medicine):
        f = general_medicine.fields["gm.cc.duration_of_concern"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, {"value": 0, "unit": "days"})
        assert exc.value.reason_code == "out_of_range"

    def test_duration_rejects_non_mapping(self, general_medicine):
        f = general_medicine.fields["gm.cc.duration_of_concern"]
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, "three days")
        assert exc.value.reason_code == "expected_duration"

    def test_number_range_and_unit(self, ayurveda):
        f = ayurveda.fields["ay.dv.pramana.height_cm"]
        assert engine.validate_and_normalize(f, {"value": 170, "unit": "cm"}) == {
            "value": 170.0, "unit": "cm"
        }
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, 500)
        assert exc.value.reason_code == "out_of_range"

    def test_body_region_multi_select(self, general_medicine):
        f = general_medicine.fields["gm.hpi.site"]
        assert engine.validate_and_normalize(f, ["chest_left"]) == {"codes": ["chest_left"]}
        with pytest.raises(AnswerValidationError) as exc:
            engine.validate_and_normalize(f, ["left_earlobe"])
        assert exc.value.reason_code == "unknown_option"

    def test_date_shape(self):
        f = Field(id="gm.t.d", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.DATE, widget=Widget.DATE_PICKER)
        assert engine.validate_and_normalize(f, "2026-03-14") == {"date": "2026-03-14"}
        for bad in ("14/03/2026", "2026-13-01", "not a date", "2026-03"):
            with pytest.raises(AnswerValidationError) as exc:
                engine.validate_and_normalize(f, bad)
            assert exc.value.reason_code == "expected_date"

    def test_frequency_accepts_code_or_envelope(self):
        f = Field(id="gm.t.f", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.FREQUENCY, widget=Widget.FREQUENCY_PICKER,
                  validation=Validation(units=("per_day", "per_week")))
        assert engine.validate_and_normalize(f, "per_day") == {"unit": "per_day"}
        assert engine.validate_and_normalize(f, {"value": 3, "unit": "per_week"}) == {
            "value": 3, "unit": "per_week"
        }
        with pytest.raises(AnswerValidationError):
            engine.validate_and_normalize(f, "per_fortnight")


class TestStateImmutability:
    def test_with_answer_returns_a_new_state(self, toy_protocol):
        original = SessionState()
        updated = original.with_answer(AnswerRecord(field_id="gm.toy.root", value={"code": "pain"}))
        assert original.answers == {}
        assert "gm.toy.root" in updated.answers

    def test_with_fast_path_preserves_answers(self, toy_protocol):
        original = answered(**{"gm.toy.root": {"code": "pain"}})
        fast = original.with_fast_path()
        assert fast.answers.keys() == original.answers.keys()
        assert original.fast_path_active is False

    def test_answer_view_excludes_non_substantive(self, toy_protocol):
        state = SessionState(answers={
            "a": AnswerRecord(field_id="a", value=1),
            "b": AnswerRecord(field_id="b", value=2,
                              skip_reason=SkipReason.NOT_ASKED_DUE_TO_EMERGENCY_ESCALATION),
            "c": AnswerRecord(field_id="c", value=None),
        })
        assert state.answer_view() == {"a": 1}


class TestContentValidation:
    def test_duplicate_field_id_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.dup.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True)
        with pytest.raises(ProtocolContentError, match="duplicate field id"):
            build_protocol(family="general_medicine", version="dup", content_checksum="x",
                           concepts=concepts, fields=[f, f])

    def test_unknown_concept_rejected(self):
        f = Field(id="gm.x.a", concept_code="missing", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True)
        with pytest.raises(ProtocolContentError, match="unknown concept"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=[Concept(code="c", category="s")], fields=[f])

    def test_dangling_dependency_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True,
                  depends_on={"op": "answered", "field": "gm.x.ghost"})
        with pytest.raises(ProtocolContentError, match="depends on unknown field"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_self_dependency_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True,
                  depends_on={"op": "answered", "field": "gm.x.a"})
        with pytest.raises(ProtocolContentError, match="depends on itself"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_dependency_cycle_rejected(self):
        """A cycle would let the engine stall with no next question."""
        concepts = [Concept(code="c", category="symptom")]
        fields = [
            Field(id="gm.cyc.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True,
                  depends_on={"op": "answered", "field": "gm.cyc.b"}),
            Field(id="gm.cyc.b", concept_code="c", category="symptom", group="g", order=2,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO,
                  depends_on={"op": "answered", "field": "gm.cyc.a"}),
        ]
        with pytest.raises(ProtocolContentError, match="dependency cycle"):
            build_protocol(family="general_medicine", version="cyc", content_checksum="x",
                           concepts=concepts, fields=fields)

    def test_select_without_options_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.SINGLE_SELECT, widget=Widget.CHOICE_GRID, ample=True)
        with pytest.raises(ProtocolContentError, match="select but has no options"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_scale_without_bounds_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.SCALE, widget=Widget.SEVERITY_FACES, ample=True)
        with pytest.raises(ProtocolContentError, match="must declare min and max"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_duplicate_option_rejected(self):
        concepts = [Concept(code="c", category="symptom")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.SINGLE_SELECT, widget=Widget.CHOICE_GRID, ample=True,
                  options=(Option(value="a"), Option(value="a")))
        with pytest.raises(ProtocolContentError, match="duplicate option"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_duplicate_concept_rejected(self):
        concepts = [Concept(code="c", category="a"), Concept(code="c", category="b")]
        f = Field(id="gm.x.a", concept_code="c", category="symptom", group="g", order=1,
                  value_type=ValueType.BOOLEAN, widget=Widget.YES_NO, ample=True)
        with pytest.raises(ProtocolContentError, match="duplicate concept code"):
            build_protocol(family="general_medicine", version="x", content_checksum="x",
                           concepts=concepts, fields=[f])

    def test_field_or_raise_reports_the_id(self, toy_protocol):
        from medikiosk.modules.clinical_protocol.model import UnknownFieldError

        with pytest.raises(UnknownFieldError) as exc:
            toy_protocol.field_or_raise("gm.toy.nope")
        assert exc.value.field_id == "gm.toy.nope"


class TestFullScriptedInterview:
    """Phase 1 DoD: scripted input completes a full deterministic interview."""

    def test_general_medicine_runs_to_completion(self, general_medicine, thresholds):
        state = SessionState()
        asked: list[str] = []
        guard = 0

        while (field := engine.next_field(general_medicine, state)) is not None:
            guard += 1
            assert guard < 200, "interview failed to terminate"
            asked.append(field.id)
            value = _synthesise_answer(field)
            normalized = engine.validate_and_normalize(field, value)
            state = state.with_answer(
                AnswerRecord(field_id=field.id, value=normalized, confidence=1.0, confirmed=True)
            )

        assert engine.completeness(general_medicine, state) == 1.0
        assert asked[0] == "gm.cc.primary_complaint"
        # No AMPLE field may appear in a routine interview (§11).
        assert not any(fid.startswith("gm.ample.") for fid in asked)
        # Ordering is monotonically non-decreasing in O.
        orders = [general_medicine.fields[fid].order for fid in asked]
        assert orders == sorted(orders)

    def test_ayurveda_runs_on_the_same_engine(self, ayurveda, thresholds):
        """Phase 6 DoD, provable at Phase 1: identical engine code path."""
        state = SessionState()
        asked: list[str] = []
        guard = 0
        while (field := engine.next_field(ayurveda, state)) is not None:
            guard += 1
            assert guard < 300
            asked.append(field.id)
            normalized = engine.validate_and_normalize(field, _synthesise_answer(field))
            state = state.with_answer(AnswerRecord(field_id=field.id, value=normalized))

        assert engine.completeness(ayurveda, state) == 1.0
        # Composition proof: an AYUSH session asks the inherited gm.* questions
        # AND the ay.* extension, from one engine.
        assert any(fid.startswith("gm.") for fid in asked)
        assert any(fid.startswith("ay.") for fid in asked)

    def test_escalation_midway_switches_to_ample_and_finishes(self, general_medicine):
        state = SessionState()
        for _ in range(4):
            field = engine.next_field(general_medicine, state)
            assert field is not None
            state = state.with_answer(AnswerRecord(
                field_id=field.id,
                value=engine.validate_and_normalize(field, _synthesise_answer(field)),
            ))

        answered_before = set(state.answered_ids())
        state = state.with_fast_path()
        assert set(state.answered_ids()) == answered_before, "§14.1: answers are never cleared"

        asked_on_fast_path: list[str] = []
        guard = 0
        while (field := engine.next_field(general_medicine, state)) is not None:
            guard += 1
            assert guard < 10, "fast path must complete in a handful of questions (§14.3)"
            asked_on_fast_path.append(field.id)
            state = state.with_answer(AnswerRecord(
                field_id=field.id,
                value=engine.validate_and_normalize(field, _synthesise_answer(field)),
            ))

        assert asked_on_fast_path, "fast path asked nothing"
        assert all(fid.startswith("gm.ample.") for fid in asked_on_fast_path)
        assert engine.completeness(general_medicine, state) == 1.0


def _synthesise_answer(field: Field):
    """Deterministic scripted answer for any field shape."""
    match field.value_type:
        case ValueType.BOOLEAN:
            return True
        case ValueType.SINGLE_SELECT:
            return field.options[0].value
        case ValueType.MULTI_SELECT | ValueType.BODY_REGION:
            return [field.options[0].value]
        case ValueType.SCALE:
            return int(field.validation.min or 0)
        case ValueType.NUMBER:
            unit = field.validation.units[0] if field.validation.units else None
            value = field.validation.min if field.validation.min is not None else 1
            return {"value": value, "unit": unit} if unit else value
        case ValueType.DURATION:
            return {"value": 2, "unit": (field.validation.units or ("days",))[0]}
        case ValueType.FREQUENCY:
            return (field.validation.units or ("per_day",))[0]
        case ValueType.DATE:
            return "2026-01-15"
        case ValueType.TEXT:
            return "scripted answer"
    raise AssertionError(f"unhandled value type {field.value_type}")
