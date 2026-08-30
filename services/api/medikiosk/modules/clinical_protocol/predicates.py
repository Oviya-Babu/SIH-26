"""Dependency predicate evaluation (CLAUDE.md §10, the ``D`` of the tuple).

A deliberately tiny, total, side-effect-free expression language. It is *not* a
general scripting language, and that is the point: governed clinical content must
be reviewable by a clinician on the Governance Board, and every expression here
reduces to a question a clinician can read aloud.

Every operator is total — an unanswered field yields ``False``, never an
exception — so a partially answered session always has a well-defined required
set. Unknown operators raise, so a typo in governed content fails loudly at load
rather than silently skipping a question.
"""

from __future__ import annotations

from typing import Any, Mapping

from medikiosk.modules.clinical_protocol.model import ProtocolContentError

# The answer view the engine evaluates against: field_id -> normalized value.
AnswerView = Mapping[str, Any]

_MISSING = object()


def evaluate(predicate: Mapping[str, Any] | None, answers: AnswerView) -> bool:
    """Evaluate a dependency predicate. ``None`` means unconditionally true."""
    if predicate is None:
        return True
    if not isinstance(predicate, Mapping):
        raise ProtocolContentError(f"predicate must be an object, got {type(predicate).__name__}")

    op = predicate.get("op")
    if not isinstance(op, str):
        raise ProtocolContentError("predicate is missing 'op'")

    handler = _HANDLERS.get(op)
    if handler is None:
        raise ProtocolContentError(f"unknown predicate operator: {op}")
    return handler(predicate, answers)


# -----------------------------------------------------------------------------
# Logical combinators
# -----------------------------------------------------------------------------
def _op_and(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return all(evaluate(arg, answers) for arg in _args(node))


def _op_or(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return any(evaluate(arg, answers) for arg in _args(node))


def _op_not(node: Mapping[str, Any], answers: AnswerView) -> bool:
    args = _args(node)
    if len(args) != 1:
        raise ProtocolContentError("'not' takes exactly one argument")
    return not evaluate(args[0], answers)


# -----------------------------------------------------------------------------
# Field predicates
# -----------------------------------------------------------------------------
def _op_answered(node: Mapping[str, Any], answers: AnswerView) -> bool:
    value = _lookup(node, answers)
    if value is _MISSING or value is None:
        return False
    if isinstance(value, (str, list, tuple, dict)):
        return len(value) > 0
    return True


def _op_equals(node: Mapping[str, Any], answers: AnswerView) -> bool:
    # Argument validation happens BEFORE the lookup so that malformed governed
    # content fails loudly even when the field happens to be unanswered.
    expected = _expected(node)
    value = _lookup(node, answers)
    return value is not _MISSING and _scalar(value) == expected


def _op_not_equals(node: Mapping[str, Any], answers: AnswerView) -> bool:
    expected = _expected(node)
    value = _lookup(node, answers)
    # An unanswered field is not "not equal" — it is simply unknown. Returning
    # False here keeps the required set from growing on unanswered questions.
    return value is not _MISSING and _scalar(value) != expected


def _op_in(node: Mapping[str, Any], answers: AnswerView) -> bool:
    allowed = set(_values(node))
    value = _lookup(node, answers)
    if value is _MISSING:
        return False
    # Set intersection over the answer's scalar members. This is the operator
    # red-flag rules use against multi-select fields, whose normalized form is
    # {"codes": [...]} — so the envelope must be flattened, not compared whole.
    return bool(_scalar_set(value) & allowed)


def _op_not_in(node: Mapping[str, Any], answers: AnswerView) -> bool:
    _values(node)
    value = _lookup(node, answers)
    if value is _MISSING:
        return False
    return not _op_in(node, answers)


def _op_contains(node: Mapping[str, Any], answers: AnswerView) -> bool:
    """Multi-select containment, and substring for a free-text answer."""
    expected = _expected(node)
    value = _lookup(node, answers)
    if value is _MISSING or value is None:
        return False
    if isinstance(value, str) and isinstance(expected, str):
        return expected.lower() in value.lower()
    # A free-text answer arrives as {"text": "..."} once normalized.
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        if isinstance(expected, str):
            return expected.lower() in value["text"].lower()
    return expected in _scalar_set(value)


def _op_gt(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return _numeric_compare(node, answers, lambda a, b: a > b)


def _op_gte(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return _numeric_compare(node, answers, lambda a, b: a >= b)


def _op_lt(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return _numeric_compare(node, answers, lambda a, b: a < b)


def _op_lte(node: Mapping[str, Any], answers: AnswerView) -> bool:
    return _numeric_compare(node, answers, lambda a, b: a <= b)


def _op_is_true(node: Mapping[str, Any], answers: AnswerView) -> bool:
    value = _lookup(node, answers)
    return value is True or _scalar(value) is True


def _op_is_false(node: Mapping[str, Any], answers: AnswerView) -> bool:
    value = _lookup(node, answers)
    return value is False or _scalar(value) is False


_HANDLERS: dict[str, Any] = {
    "and": _op_and,
    "or": _op_or,
    "not": _op_not,
    "answered": _op_answered,
    "equals": _op_equals,
    "not_equals": _op_not_equals,
    "in": _op_in,
    "not_in": _op_not_in,
    "contains": _op_contains,
    "gt": _op_gt,
    "gte": _op_gte,
    "lt": _op_lt,
    "lte": _op_lte,
    "is_true": _op_is_true,
    "is_false": _op_is_false,
}

SUPPORTED_OPERATORS: frozenset[str] = frozenset(_HANDLERS)


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _args(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    args = node.get("args")
    if not isinstance(args, (list, tuple)) or not args:
        raise ProtocolContentError(f"'{node.get('op')}' requires a non-empty 'args' list")
    return list(args)


def _lookup(node: Mapping[str, Any], answers: AnswerView) -> Any:
    field_id = node.get("field")
    if not isinstance(field_id, str):
        raise ProtocolContentError(f"'{node.get('op')}' requires a 'field'")
    return answers.get(field_id, _MISSING)


def _expected(node: Mapping[str, Any]) -> Any:
    if "value" not in node:
        raise ProtocolContentError(f"'{node.get('op')}' requires a 'value'")
    return node["value"]


def _values(node: Mapping[str, Any]) -> list[Any]:
    values = node.get("values")
    if not isinstance(values, (list, tuple)) or not values:
        raise ProtocolContentError(f"'{node.get('op')}' requires a non-empty 'values' list")
    return list(values)


def _scalar_set(value: Any) -> set[Any]:
    """Flatten a normalized answer into the set of scalars it asserts.

    A single-select asserts one code; a multi-select or body-region answer
    asserts several. Membership operators work over this set so one piece of
    governed content (``{"op": "in", ...}``) reads correctly against both, and a
    multi-select envelope never reaches an unhashable comparison.
    """
    if isinstance(value, Mapping):
        for key in ("codes", "values", "selected"):
            inner = value.get(key)
            if isinstance(inner, (list, tuple, set, frozenset)):
                return {_hashable(_scalar(v)) for v in inner}
        for key in ("code", "value"):
            if key in value:
                return {_hashable(_scalar(value[key]))}
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {_hashable(_scalar(v)) for v in value}
    return {_hashable(_scalar(value))}


def _hashable(value: Any) -> Any:
    """Collapse anything unhashable to its string form rather than raising."""
    try:
        hash(value)
    except TypeError:
        return str(value)
    return value


def _scalar(value: Any) -> Any:
    """Unwrap the normalized answer envelope to a comparable scalar.

    Normalized answers are stored as JSON. A structured answer such as a duration
    is ``{"value": 3, "unit": "days"}``; a coded answer is ``{"code": "chest_pain"}``
    or the bare code. Comparisons address the code/value, so governed content
    stays readable: ``{"op": "equals", "field": "...", "value": "chest_pain"}``.
    """
    if isinstance(value, Mapping):
        for key in ("code", "value", "selected"):
            if key in value:
                return _scalar(value[key])
        return value
    return value


def _numeric_compare(node: Mapping[str, Any], answers: AnswerView, cmp: Any) -> bool:
    value = _lookup(node, answers)
    if value is _MISSING:
        return False
    left = _as_number(_scalar(value))
    right = _as_number(_expected(node))
    if left is None or right is None:
        return False
    return bool(cmp(left, right))


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def validate_predicate(predicate: Mapping[str, Any] | None) -> None:
    """Structurally validate a predicate at content-load time.

    Evaluating against an empty answer view exercises every operator's argument
    checks, so governed content with a malformed predicate is rejected at load
    rather than mid-interview in front of a patient.
    """
    if predicate is None:
        return
    _walk_validate(predicate)


def _walk_validate(node: Any) -> None:
    if isinstance(node, Mapping) and "op" in node:
        op = node["op"]
        if op not in _HANDLERS:
            raise ProtocolContentError(f"unknown predicate operator: {op}")
        if op in ("and", "or", "not"):
            for arg in _args(node):
                _walk_validate(arg)
            if op == "not" and len(_args(node)) != 1:
                raise ProtocolContentError("'not' takes exactly one argument")
            return
        if not isinstance(node.get("field"), str):
            raise ProtocolContentError(f"'{op}' requires a 'field'")
        if op in ("in", "not_in"):
            _values(node)
        elif op in ("equals", "not_equals", "contains", "gt", "gte", "lt", "lte"):
            _expected(node)
        return
    raise ProtocolContentError("predicate node must be an object with an 'op'")
