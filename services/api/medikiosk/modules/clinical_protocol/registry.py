"""Governed protocol content loading and resolution (CLAUDE.md §10).

Two responsibilities, kept separate on purpose:

``ProtocolRegistry``
    Loads versioned content from disk, validates it, and records its SHA-256.
    The checksum is what lets a running system prove the protocol it executed is
    byte-identical to what the Clinical Governance Board approved (§46, §61).

``resolve_protocol``
    Implements §10's resolution mechanism *exactly*: device fixes the tenant,
    department fixes the protocol family, tenant config fixes the version.
    Department selection at the kiosk drives protocol loading — a governed,
    versioned lookup, **never an LLM decision** [RED LINE §10].
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medikiosk.modules.clinical_protocol.model import (
    Concept,
    Field,
    Option,
    Protocol,
    ProtocolContentError,
    Validation,
    ValueType,
    Widget,
    build_protocol,
)
from medikiosk.modules.clinical_protocol.predicates import validate_predicate
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

# Protocol families this build knows how to execute. Adding Siddha/Unani/
# Homeopathy later is new protocol *data* plus an entry here — zero engine
# change (§12). That is the literal test of protocol-agnosticism.
KNOWN_FAMILIES: frozenset[str] = frozenset({"general_medicine", "ayush_ayurveda"})


@dataclass(frozen=True, slots=True)
class ProtocolDescriptor:
    family: str
    version: str
    checksum: str
    field_count: int
    required_count: int
    ample_count: int
    groups: tuple[str, ...]


class ProtocolRegistry:
    """Immutable, process-lifetime cache of governed protocol content."""

    def __init__(self, content_root: Path) -> None:
        self._root = content_root
        self._cache: dict[str, Protocol] = {}

    @property
    def content_root(self) -> Path:
        return self._root

    def load(self, family: str, version: str) -> Protocol:
        if family not in KNOWN_FAMILIES:
            raise ProtocolContentError(f"unknown protocol family: {family}")
        key = f"{family}:{version}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self._root / "protocols" / family / f"{version}.json"
        if not path.is_file():
            raise ProtocolContentError(f"protocol content not found: {family}:{version}")

        raw_bytes = path.read_bytes()
        try:
            document = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            raise ProtocolContentError(f"protocol {key} is not valid JSON: {exc}") from exc

        # Composition: an AYUSH protocol is General Medicine's field set plus the
        # Dashavidha/Ahara-Vihara extension (§12). Sharing the base fields is what
        # makes "same engine, second instantiation" literally true, and it means
        # SOCRATES is authored, reviewed and translated exactly once.
        document, checksum = self._resolve_inheritance(document, raw_bytes, family, version)

        protocol = _parse_protocol(document, family=family, version=version, checksum=checksum)
        self._cache[key] = protocol
        log.info(
            "protocol_loaded",
            component="clinical_protocol",
            protocol_family=family,
            protocol_version=version,
            count=len(protocol.fields),
        )
        return protocol

    def _resolve_inheritance(
        self,
        document: dict[str, Any],
        raw_bytes: bytes,
        family: str,
        version: str,
    ) -> tuple[dict[str, Any], str]:
        """Merge an ``extends`` base into ``document``.

        The checksum covers the base content as well as the deriving content, so
        governance approval of the composed protocol is what is actually verified
        at runtime — editing the base cannot silently change a derived protocol's
        recorded checksum.
        """
        extends = document.get("extends")
        if not extends:
            return document, hashlib.sha256(raw_bytes).hexdigest()

        base_family = str(extends.get("family", ""))
        base_version = str(extends.get("version", ""))
        if base_family == family and base_version == version:
            raise ProtocolContentError(f"protocol {family}:{version} extends itself")

        base_path = self._root / "protocols" / base_family / f"{base_version}.json"
        if not base_path.is_file():
            raise ProtocolContentError(
                f"protocol {family}:{version} extends missing base {base_family}:{base_version}"
            )
        base_bytes = base_path.read_bytes()
        base_doc = json.loads(base_bytes)
        if base_doc.get("extends"):
            raise ProtocolContentError(
                "protocol inheritance is single-level by design: a governance reviewer "
                "must be able to read the whole protocol in two files"
            )

        excluded = set(document.get("exclude_fields", []) or ())
        overrides = {
            str(o["id"]): o for o in document.get("override_fields", []) or () if "id" in o
        }
        unknown_base_ids = (excluded | set(overrides)) - {
            str(f.get("id")) for f in base_doc.get("fields", [])
        }
        if unknown_base_ids:
            raise ProtocolContentError(
                "exclude_fields/override_fields reference field ids absent from the base: "
                + ", ".join(sorted(unknown_base_ids))
            )

        merged_fields: list[dict[str, Any]] = []
        for base_field in base_doc.get("fields", []):
            fid = str(base_field.get("id"))
            if fid in excluded:
                continue
            if fid in overrides:
                base_field = {**base_field, **overrides[fid]}
            merged_fields.append(base_field)
        merged_fields.extend(document.get("fields", []))

        base_concepts = {str(c["code"]): c for c in base_doc.get("concepts", [])}
        for concept in document.get("concepts", []):
            base_concepts[str(concept["code"])] = concept

        composed = {
            **document,
            "concepts": list(base_concepts.values()),
            "fields": merged_fields,
        }
        composed.pop("extends", None)
        composed.pop("exclude_fields", None)
        composed.pop("override_fields", None)

        checksum = hashlib.sha256(base_bytes + b"\x00" + raw_bytes).hexdigest()
        return composed, checksum

    def load_all(self) -> tuple[Protocol, ...]:
        """Eagerly load every available protocol, so bad content fails at start."""
        loaded: list[Protocol] = []
        protocols_dir = self._root / "protocols"
        if not protocols_dir.is_dir():
            raise ProtocolContentError(f"protocol content directory missing: {protocols_dir}")
        for family_dir in sorted(protocols_dir.iterdir()):
            if not family_dir.is_dir():
                continue
            for version_file in sorted(family_dir.glob("*.json")):
                loaded.append(self.load(family_dir.name, version_file.stem))
        if not loaded:
            raise ProtocolContentError("no protocol content found")
        return tuple(loaded)

    def describe(self) -> tuple[ProtocolDescriptor, ...]:
        return tuple(
            ProtocolDescriptor(
                family=p.family,
                version=p.version,
                checksum=p.content_checksum,
                field_count=len(p.fields),
                required_count=sum(1 for f in p.fields.values() if f.required),
                ample_count=len(p.ample_fields),
                groups=p.groups,
            )
            for p in sorted(self._cache.values(), key=lambda p: p.key)
        )


def _parse_protocol(
    document: dict[str, Any],
    *,
    family: str,
    version: str,
    checksum: str,
) -> Protocol:
    if document.get("protocol_family") != family:
        raise ProtocolContentError(
            f"content declares family {document.get('protocol_family')!r}, expected {family!r}"
        )
    if str(document.get("version")) != version:
        raise ProtocolContentError(
            f"content declares version {document.get('version')!r}, expected {version!r}"
        )

    # Governed content must not carry patient-facing text: that would put
    # translation and clinical logic in the same review, and would let a
    # localization change alter clinical branching (§10).
    _assert_no_display_text(document)

    concepts = [
        Concept(
            code=_require_str(c, "code", "concept"),
            category=_require_str(c, "category", "concept"),
            nlu_slot=c.get("nlu_slot"),
        )
        for c in document.get("concepts", [])
    ]
    if not concepts:
        raise ProtocolContentError(f"protocol {family}:{version} declares no concepts")

    fields: list[Field] = []
    for raw in document.get("fields", []):
        fields.append(_parse_field(raw, family=family, version=version))
    if not fields:
        raise ProtocolContentError(f"protocol {family}:{version} declares no fields")

    return build_protocol(
        family=family,
        version=version,
        content_checksum=checksum,
        concepts=concepts,
        fields=fields,
    )


def _parse_field(raw: dict[str, Any], *, family: str, version: str) -> Field:
    field_id = _require_str(raw, "id", "field")
    # Field ids are globally stable and namespace-prefixed by the protocol family
    # that *authored* them. A composed protocol therefore keeps inherited ids
    # unchanged (gm.*), which is precisely why their translations are reused.
    if not any(field_id.startswith(prefix) for prefix in FAMILY_PREFIXES.values()):
        raise ProtocolContentError(
            f"field id {field_id!r} must begin with a registered family prefix "
            f"({', '.join(sorted(FAMILY_PREFIXES.values()))})"
        )

    try:
        value_type = ValueType(_require_str(raw, "value_type", "field"))
        widget = Widget(_require_str(raw, "widget", "field"))
    except ValueError as exc:
        raise ProtocolContentError(f"field {field_id}: {exc}") from exc

    depends_on = raw.get("depends_on")
    validate_predicate(depends_on)

    validation_raw = raw.get("validation", {}) or {}
    validation = Validation(
        min=validation_raw.get("min"),
        max=validation_raw.get("max"),
        max_length=validation_raw.get("max_length"),
        pattern=validation_raw.get("pattern"),
        units=tuple(validation_raw.get("units", ()) or ()),
    )

    options = tuple(
        Option(
            value=_require_str(o, "value", f"option of {field_id}"),
            icon=o.get("icon"),
            critical=bool(o.get("critical", False)),
            exclusive=bool(o.get("exclusive", False)),
        )
        for o in raw.get("options", []) or ()
    )

    return Field(
        id=field_id,
        concept_code=_require_str(raw, "concept_code", "field"),
        category=_require_str(raw, "category", "field"),
        group=_require_str(raw, "group", "field"),
        order=int(_require(raw, "order", "field")),
        value_type=value_type,
        widget=widget,
        required=bool(raw.get("required", False)),
        options=options,
        depends_on=depends_on,
        validation=validation,
        confirm_back=bool(raw.get("confirm_back", False)),
        tau_high=_optional_float(raw.get("tau_high")),
        tau_low=_optional_float(raw.get("tau_low")),
        ample=bool(raw.get("ample", False)),
        red_flag_input=bool(raw.get("red_flag_input", False)),
        unlocks=tuple(raw.get("unlocks", ()) or ()),
    )


_DISPLAY_TEXT_KEYS = frozenset(
    {
        "label",
        "labels",
        "text",
        "prompt",
        "voice_prompt",
        "voicePrompt",
        "touch_label",
        "touchLabel",
        "help_text",
        "helpText",
        "description",
        "display_name",
        "message",
        "placeholder",
    }
)


def _assert_no_display_text(node: Any, path: str = "$") -> None:
    """Refuse content that embeds patient-facing strings.

    The legacy prototype inlined ``{"en": ..., "hi": ...}`` per question. That
    couples clinical review to translation review and puts language inside the
    engine's input. Localized strings belong in the i18n resources, keyed by the
    stable field/option id.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _DISPLAY_TEXT_KEYS:
                raise ProtocolContentError(
                    f"{path}.{key}: patient-facing text must not live in protocol content; "
                    "put it in content/i18n/<lang>/protocol.<family>.json keyed by field id"
                )
            _assert_no_display_text(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_no_display_text(item, f"{path}[{index}]")


FAMILY_PREFIXES: dict[str, str] = {
    "general_medicine": "gm.",
    "ayush_ayurveda": "ay.",
}


def family_for_field(field_id: str) -> str:
    """Which family's i18n bundle owns a field's translations."""
    for family, prefix in FAMILY_PREFIXES.items():
        if field_id.startswith(prefix):
            return family
    raise ProtocolContentError(f"field id {field_id!r} has no registered family prefix")


def _require(raw: dict[str, Any], key: str, what: str) -> Any:
    if key not in raw:
        raise ProtocolContentError(f"{what} is missing required key {key!r}")
    return raw[key]


def _require_str(raw: dict[str, Any], key: str, what: str) -> str:
    value = _require(raw, key, what)
    if not isinstance(value, str) or not value:
        raise ProtocolContentError(f"{what}.{key} must be a non-empty string")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise ProtocolContentError("confidence threshold must be within [0, 1]")
    return parsed
