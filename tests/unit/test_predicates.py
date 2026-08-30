"""Dependency predicate evaluation (CLAUDE.md §10, the D of the Protocol tuple).

Branch coverage matters here beyond the usual reason: a predicate bug does not
crash, it silently asks the wrong question or skips a required one. Every
operator is therefore tested in both directions, plus the unanswered case, which
is the one that governs whether a partially-completed interview has a
well-defined required set.
"""

from __future__ import annotations

import pytest

from medikiosk.modules.clinical_protocol import predicates
from medikiosk.modules.clinical_protocol.model import ProtocolContentError

pytestmark = pytest.mark.unit


def ev(predicate, answers=None):
    return predicates.evaluate(predicate, answers or {})


class TestTotality:
    def test_none_predicate_is_unconditionally_true(self):
        assert ev(None) is True

    @pytest.mark.parametrize(
        "op",
        [
            "answered",
            "equals",
            "not_equals",
            "in",
            "not_in",
            "contains",
            "gt",
            "gte",
            "lt",
            "lte",
            "is_true",
            "is_false",
        ],
    )
    def test_every_field_operator_is_false_on_unanswered(self, op):
        """An unanswered field must never satisfy a dependency.

        If it did, the fast path (which carries answers forward but not skips)
        could unlock questions nobody was ever asked.
        """
        node = {"op": op, "field": "missing", "value": "x", "values": ["x"]}
        assert ev(node) is False

    def test_unknown_operator_raises(self):
        with pytest.raises(ProtocolContentError, match="unknown predicate operator"):
            ev({"op": "sql_injection", "field": "a", "value": 1})

    def test_missing_op_raises(self):
        with pytest.raises(ProtocolContentError, match="missing 'op'"):
            ev({"field": "a"})

    def test_non_mapping_predicate_raises(self):
        with pytest.raises(ProtocolContentError, match="must be an object"):
            ev(["not", "a", "predicate"])


class TestLogicalCombinators:
    def test_and_requires_all(self):
        answers = {"a": True, "b": False}
        assert ev({"op": "and", "args": [
            {"op": "is_true", "field": "a"},
            {"op": "is_false", "field": "b"},
        ]}, answers) is True
        assert ev({"op": "and", "args": [
            {"op": "is_true", "field": "a"},
            {"op": "is_true", "field": "b"},
        ]}, answers) is False

    def test_or_requires_any(self):
        answers = {"a": True, "b": False}
        assert ev({"op": "or", "args": [
            {"op": "is_true", "field": "b"},
            {"op": "is_true", "field": "a"},
        ]}, answers) is True
        assert ev({"op": "or", "args": [
            {"op": "is_true", "field": "b"},
            {"op": "answered", "field": "z"},
        ]}, answers) is False

    def test_not_inverts(self):
        assert ev({"op": "not", "args": [{"op": "answered", "field": "a"}]}, {"a": 1}) is False
        assert ev({"op": "not", "args": [{"op": "answered", "field": "a"}]}, {}) is True

    def test_not_rejects_multiple_arguments(self):
        with pytest.raises(ProtocolContentError, match="exactly one argument"):
            ev({"op": "not", "args": [
                {"op": "answered", "field": "a"},
                {"op": "answered", "field": "b"},
            ]})

    def test_empty_args_rejected(self):
        with pytest.raises(ProtocolContentError, match="non-empty 'args'"):
            ev({"op": "and", "args": []})

    def test_nesting_depth(self):
        node = {
            "op": "and",
            "args": [
                {"op": "or", "args": [
                    {"op": "equals", "field": "x", "value": "p"},
                    {"op": "equals", "field": "x", "value": "q"},
                ]},
                {"op": "not", "args": [{"op": "is_true", "field": "y"}]},
            ],
        }
        assert ev(node, {"x": "q", "y": False}) is True
        assert ev(node, {"x": "q", "y": True}) is False
        assert ev(node, {"x": "r", "y": False}) is False


class TestAnswered:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, False),
            ("", False),
            ([], False),
            ({}, False),
            (0, True),        # zero severity IS an answer
            (False, True),    # an explicit "no" IS an answer
            ("x", True),
            (["a"], True),
            ({"code": "a"}, True),
        ],
    )
    def test_answered_semantics(self, value, expected):
        assert ev({"op": "answered", "field": "f"}, {"f": value}) is expected


class TestEnvelopeUnwrapping:
    """Normalized answers are JSON envelopes; content compares plain codes."""

    def test_code_envelope(self):
        assert ev({"op": "equals", "field": "f", "value": "chest_pain"},
                  {"f": {"code": "chest_pain"}}) is True

    def test_value_envelope(self):
        assert ev({"op": "gte", "field": "f", "value": 8}, {"f": {"value": 9}}) is True

    def test_bare_scalar(self):
        assert ev({"op": "equals", "field": "f", "value": "x"}, {"f": "x"}) is True

    def test_multi_select_codes_envelope_with_in(self):
        """The exact shape red-flag rules evaluate against (§14).

        A multi-select answer normalizes to ``{"codes": [...]}``, and safety
        rules match it with ``in``. If the envelope were compared whole, every
        such rule would silently never fire.
        """
        node = {"op": "in", "field": "f", "values": ["crushing", "pressing"]}
        assert ev(node, {"f": {"codes": ["burning", "pressing"]}}) is True
        assert ev(node, {"f": {"codes": ["burning", "sharp"]}}) is False
        assert ev(node, {"f": ["burning", "pressing"]}) is True

    def test_multi_select_codes_envelope_with_contains(self):
        node = {"op": "contains", "field": "f", "value": "crushing"}
        assert ev(node, {"f": {"codes": ["crushing", "sharp"]}}) is True
        assert ev(node, {"f": {"codes": ["sharp"]}}) is False

    def test_contains_on_normalized_text_envelope(self):
        node = {"op": "contains", "field": "f", "value": "metformin"}
        assert ev(node, {"f": {"text": "Metformin 500mg BD"}}) is True
        assert ev(node, {"f": {"text": "amlodipine 5mg"}}) is False


class TestComparisons:
    def test_equals_and_not_equals(self):
        assert ev({"op": "equals", "field": "f", "value": "a"}, {"f": "a"}) is True
        assert ev({"op": "not_equals", "field": "f", "value": "a"}, {"f": "b"}) is True
        assert ev({"op": "not_equals", "field": "f", "value": "a"}, {"f": "a"}) is False

    def test_in_with_list_answer_intersects(self):
        node = {"op": "in", "field": "f", "values": ["x", "y"]}
        assert ev(node, {"f": ["y", "z"]}) is True
        assert ev(node, {"f": ["z"]}) is False

    def test_not_in(self):
        node = {"op": "not_in", "field": "f", "values": ["x"]}
        assert ev(node, {"f": "y"}) is True
        assert ev(node, {"f": "x"}) is False

    def test_contains_multi_select(self):
        node = {"op": "contains", "field": "f", "value": "x"}
        assert ev(node, {"f": ["x", "y"]}) is True
        assert ev(node, {"f": ["y"]}) is False

    def test_contains_substring_on_text(self):
        assert ev({"op": "contains", "field": "f", "value": "Metformin"},
                  {"f": "metformin 500mg twice daily"}) is True

    @pytest.mark.parametrize(
        "op,value,answer,expected",
        [
            ("gt", 7, 8, True),
            ("gt", 7, 7, False),
            ("gte", 7, 7, True),
            ("gte", 7, 6, False),
            ("lt", 3, 2, True),
            ("lt", 3, 3, False),
            ("lte", 3, 3, True),
            ("lte", 3, 4, False),
        ],
    )
    def test_numeric_operators(self, op, value, answer, expected):
        assert ev({"op": op, "field": "f", "value": value}, {"f": answer}) is expected

    def test_numeric_operators_coerce_numeric_strings(self):
        assert ev({"op": "gte", "field": "f", "value": 8}, {"f": "9"}) is True

    def test_numeric_operators_reject_non_numeric(self):
        assert ev({"op": "gte", "field": "f", "value": 8}, {"f": "severe"}) is False

    def test_boolean_is_not_a_number(self):
        """True must not compare as 1 — a yes/no answer is not a severity."""
        assert ev({"op": "gte", "field": "f", "value": 1}, {"f": True}) is False

    def test_is_true_is_false(self):
        assert ev({"op": "is_true", "field": "f"}, {"f": True}) is True
        assert ev({"op": "is_false", "field": "f"}, {"f": False}) is True
        assert ev({"op": "is_true", "field": "f"}, {"f": False}) is False


class TestArgumentValidation:
    def test_field_operator_requires_field(self):
        with pytest.raises(ProtocolContentError, match="requires a 'field'"):
            ev({"op": "equals", "value": "x"})

    def test_equals_requires_value(self):
        with pytest.raises(ProtocolContentError, match="requires a 'value'"):
            ev({"op": "equals", "field": "f"})

    def test_in_requires_values(self):
        with pytest.raises(ProtocolContentError, match="requires a non-empty 'values'"):
            ev({"op": "in", "field": "f"})


class TestReferencedFields:
    def test_collects_nested_field_references(self):
        node = {
            "op": "and",
            "args": [
                {"op": "equals", "field": "a", "value": 1},
                {"op": "or", "args": [
                    {"op": "answered", "field": "b"},
                    {"op": "not", "args": [{"op": "is_true", "field": "c"}]},
                ]},
            ],
        }
        from medikiosk.modules.clinical_protocol.model import referenced_fields

        assert referenced_fields(node) == frozenset({"a", "b", "c"})

    def test_none_yields_empty(self):
        from medikiosk.modules.clinical_protocol.model import referenced_fields

        assert referenced_fields(None) == frozenset()


class TestStructuralValidation:
    def test_validate_accepts_well_formed(self):
        predicates.validate_predicate({"op": "in", "field": "f", "values": ["a"]})
        predicates.validate_predicate(None)

    def test_validate_rejects_unknown_operator(self):
        with pytest.raises(ProtocolContentError):
            predicates.validate_predicate({"op": "nope", "field": "f"})

    def test_validate_rejects_missing_field(self):
        with pytest.raises(ProtocolContentError):
            predicates.validate_predicate({"op": "equals", "value": 1})

    def test_validate_rejects_bare_leaf(self):
        with pytest.raises(ProtocolContentError, match="must be an object with an 'op'"):
            predicates.validate_predicate({"field": "f"})

    def test_validate_recurses_into_args(self):
        with pytest.raises(ProtocolContentError):
            predicates.validate_predicate(
                {"op": "and", "args": [{"op": "bogus", "field": "f"}]}
            )
