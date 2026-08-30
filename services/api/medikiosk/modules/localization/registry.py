"""Localization layer (CLAUDE.md §10, §18).

The i18n boundary. Everything a patient reads or hears is resolved here, from a
stable clinical id:

    Clinical Concept → Protocol Field → Localization → UI / Voice

The engine never sees a string in a human language, and this module never makes a
clinical decision. That separation is what lets a new language ship by adding
translation, ASR and TTS resources — no clinical logic changes, no re-review of
branching (§59).

Exactly five languages are supported in this prototype. The set is a single
constant, and :meth:`LocalizationRegistry.assert_complete` fails the build if any
patient-facing protocol field is missing any of them, so an untranslated question
can never reach a patient at the kiosk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

from medikiosk.modules.clinical_protocol.model import Field, Protocol
from medikiosk.modules.clinical_protocol.registry import family_for_field
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    """A supported language and the AI resources it is wired to (§18.1)."""

    code: str
    # Endonym, shown in the language chooser in the language's own script — a
    # patient who cannot read English must still recognise their language (§1).
    endonym: str
    english_name: str
    script: str
    # Bhashini/AI4Bharat language identifiers for ASR and TTS.
    asr_locale: str
    tts_locale: str
    tts_voice: str
    # Right-to-left is not needed for these five, but the field exists so adding
    # Urdu (Unani protocols) later is a resource change, not a layout rewrite.
    rtl: bool = False


# ---------------------------------------------------------------------------
# The five prototype languages. Adding a sixth means: one entry here, one
# content/i18n/<code>/ directory, and ASR/TTS resource identifiers. Nothing in
# the clinical engine changes.
# ---------------------------------------------------------------------------
LANGUAGES: tuple[LanguageProfile, ...] = (
    LanguageProfile(
        code="en",
        endonym="English",
        english_name="English",
        script="Latn",
        asr_locale="en-IN",
        tts_locale="en-IN",
        tts_voice="bhashini:en-IN:female",
    ),
    LanguageProfile(
        code="hi",
        endonym="हिन्दी",
        english_name="Hindi",
        script="Deva",
        asr_locale="hi-IN",
        tts_locale="hi-IN",
        tts_voice="bhashini:hi-IN:female",
    ),
    LanguageProfile(
        code="ta",
        endonym="தமிழ்",
        english_name="Tamil",
        script="Taml",
        asr_locale="ta-IN",
        tts_locale="ta-IN",
        tts_voice="bhashini:ta-IN:female",
    ),
    LanguageProfile(
        code="te",
        endonym="తెలుగు",
        english_name="Telugu",
        script="Telu",
        asr_locale="te-IN",
        tts_locale="te-IN",
        tts_voice="bhashini:te-IN:female",
    ),
    LanguageProfile(
        code="ml",
        endonym="മലയാളം",
        english_name="Malayalam",
        script="Mlym",
        asr_locale="ml-IN",
        tts_locale="ml-IN",
        tts_voice="bhashini:ml-IN:female",
    ),
)

SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(p.code for p in LANGUAGES)
DEFAULT_LANGUAGE = "en"
LANGUAGE_BY_CODE: Mapping[str, LanguageProfile] = {p.code: p for p in LANGUAGES}

# Bundles the kiosk and the API resolve strings from.
BUNDLES: tuple[str, ...] = (
    "kiosk",       # chrome: buttons, progress, loading, offline, completion
    "errors",      # reason_code -> patient-appropriate message
    "consent",     # consent notice + audio narration script (§7.2)
    "clinical",    # shared clinical vocabulary: groups, categories, units
    "protocol.general_medicine",
    "protocol.ayush_ayurveda",
)


class LocalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalizedOption:
    value: str
    label: str
    icon: str | None
    help: str | None = None


@dataclass(frozen=True, slots=True)
class LocalizedField:
    """A protocol field, rendered for one language.

    ``voice_prompt`` is what TTS speaks; ``touch_label`` is the short on-screen
    question; ``help`` is the optional plain-language expansion. They are separate
    strings because spoken and written registers differ, and a low-literacy user
    depends on the spoken one being conversational (§1, §18).
    """

    field_id: str
    language: str
    voice_prompt: str
    touch_label: str
    help: str | None
    confirm_prompt: str | None
    options: tuple[LocalizedOption, ...]
    unit_labels: Mapping[str, str]
    # True when the string came from the English fallback rather than the
    # requested language. Surfaced so a gap is visible, never silent.
    fallback_used: bool = False


class LocalizationRegistry:
    """Loads and serves the externalized translation resources."""

    def __init__(self, content_root: Path) -> None:
        self._root = content_root / "i18n"
        self._bundles: dict[tuple[str, str], dict[str, Any]] = {}

    # -- loading -----------------------------------------------------------
    def load_all(self) -> None:
        missing_dirs = [
            code for code in SUPPORTED_LANGUAGES if not (self._root / code).is_dir()
        ]
        if missing_dirs:
            raise LocalizationError(
                "missing i18n directories for: " + ", ".join(missing_dirs)
            )
        for code in SUPPORTED_LANGUAGES:
            for bundle in BUNDLES:
                path = self._root / code / f"{bundle}.json"
                if not path.is_file():
                    raise LocalizationError(f"missing translation bundle: {code}/{bundle}.json")
                try:
                    self._bundles[(code, bundle)] = json.loads(path.read_text("utf-8"))
                except json.JSONDecodeError as exc:
                    raise LocalizationError(f"{code}/{bundle}.json is not valid JSON: {exc}") from exc
        log.info(
            "localization_loaded",
            component="localization",
            count=len(self._bundles),
        )

    @cached_property
    def languages(self) -> tuple[LanguageProfile, ...]:
        return LANGUAGES

    def profile(self, language: str) -> LanguageProfile:
        try:
            return LANGUAGE_BY_CODE[language]
        except KeyError as exc:
            raise LocalizationError(f"unsupported language: {language}") from exc

    def normalize(self, language: str | None) -> str:
        """Coerce an arbitrary request value to a supported language code."""
        if not language:
            return DEFAULT_LANGUAGE
        code = language.strip().lower().replace("_", "-").split("-")[0]
        return code if code in LANGUAGE_BY_CODE else DEFAULT_LANGUAGE

    # -- string lookup -----------------------------------------------------
    def text(
        self,
        language: str,
        bundle: str,
        key: str,
        *,
        default: str | None = None,
        **params: Any,
    ) -> str:
        """Resolve one string, falling back to English, then to ``default``."""
        value = self._lookup(language, bundle, key)
        if value is None and language != DEFAULT_LANGUAGE:
            value = self._lookup(DEFAULT_LANGUAGE, bundle, key)
        if value is None:
            if default is not None:
                return default
            raise LocalizationError(f"missing translation key: {bundle}:{key}")
        return _interpolate(value, params)

    def bundle(self, language: str, bundle: str) -> dict[str, Any]:
        """The whole bundle, English-merged, for a frontend to consume."""
        base = dict(self._bundles.get((DEFAULT_LANGUAGE, bundle), {}))
        if language != DEFAULT_LANGUAGE:
            base = _deep_merge(base, self._bundles.get((language, bundle), {}))
        return base

    def _lookup(self, language: str, bundle: str, key: str) -> Any:
        """Dotted-path lookup, for chrome bundles whose keys contain no dots."""
        return self._lookup_path(language, bundle, tuple(key.split(".")))

    def _lookup_path(self, language: str, bundle: str, path: tuple[str, ...]) -> Any:
        """Lookup by LITERAL path segments.

        Field ids contain dots (``gm.hpi.severity``), so a dotted-string lookup
        cannot address them — it would split the id itself. Protocol lookups
        therefore pass the id as one literal segment.
        """
        data: Any = self._bundles.get((language, bundle))
        if data is None:
            return None
        for part in path:
            if not isinstance(data, dict) or part not in data:
                return None
            data = data[part]
        return data if isinstance(data, str) else None

    # -- protocol field rendering -----------------------------------------
    def render_field(self, protocol: Protocol, f: Field, language: str) -> LocalizedField:
        # Translations follow the field id, not the executing protocol: an AYUSH
        # session asking an inherited gm.* SOCRATES question reuses the General
        # Medicine bundle, so that question is translated exactly once.
        bundle = f"protocol.{family_for_field(f.id)}"
        base = ("fields", f.id)
        fallback = False

        voice = self._lookup_path(language, bundle, (*base, "voice_prompt"))
        touch = self._lookup_path(language, bundle, (*base, "touch_label"))
        help_text = self._lookup_path(language, bundle, (*base, "help"))
        confirm = self._lookup_path(language, bundle, (*base, "confirm"))

        if voice is None or touch is None:
            fallback = True
            voice = voice or self._lookup_path(DEFAULT_LANGUAGE, bundle, (*base, "voice_prompt"))
            touch = touch or self._lookup_path(DEFAULT_LANGUAGE, bundle, (*base, "touch_label"))
        if help_text is None:
            help_text = self._lookup_path(DEFAULT_LANGUAGE, bundle, (*base, "help"))
        if confirm is None:
            confirm = self._lookup_path(DEFAULT_LANGUAGE, bundle, (*base, "confirm"))

        if voice is None or touch is None:
            raise LocalizationError(
                f"field {f.id} has no localized prompt in {language!r} or English"
            )

        options: list[LocalizedOption] = []
        for opt in f.options:
            # Resolution order: field-specific override, then shared clinical
            # vocabulary, then the English forms of each. A code such as
            # 'diabetes' therefore lives in exactly one place per language
            # unless a question genuinely needs different wording.
            label = self._lookup_path(language, bundle, (*base, "options", opt.value))
            if label is None:
                label = self._lookup_path(language, "clinical", ("options", opt.value))
            if label is None:
                label = self._lookup_path(DEFAULT_LANGUAGE, bundle, (*base, "options", opt.value))
                if label is None:
                    label = self._lookup_path(
                        DEFAULT_LANGUAGE, "clinical", ("options", opt.value)
                    )
                if label is not None and language != DEFAULT_LANGUAGE:
                    fallback = True
            if label is None:
                raise LocalizationError(
                    f"option {opt.value!r} of field {f.id} has no localized label"
                )
            options.append(
                LocalizedOption(
                    value=opt.value,
                    label=label,
                    icon=opt.icon,
                    help=self._lookup_path(language, bundle, (*base, "option_help", opt.value)),
                )
            )

        unit_labels: dict[str, str] = {}
        for unit in f.validation.units:
            unit_labels[unit] = (
                self._lookup(language, "clinical", f"units.{unit}")
                or self._lookup(DEFAULT_LANGUAGE, "clinical", f"units.{unit}")
                or unit
            )

        return LocalizedField(
            field_id=f.id,
            language=language,
            voice_prompt=voice,
            touch_label=touch,
            help=help_text,
            confirm_prompt=confirm,
            options=tuple(options),
            unit_labels=unit_labels,
            fallback_used=fallback,
        )

    def group_label(self, protocol: Protocol, group: str, language: str) -> str:
        for lang in (language, DEFAULT_LANGUAGE):
            for bundle in (f"protocol.{protocol.family}", "clinical"):
                found = self._lookup_path(lang, bundle, ("groups", group))
                if found is not None:
                    return found
        return group

    # -- completeness gate -------------------------------------------------
    def missing_translations(self, protocol: Protocol) -> dict[str, list[str]]:
        """Every (language, key) gap for a protocol's patient-facing content."""
        gaps: dict[str, list[str]] = {}
        own_bundle = f"protocol.{protocol.family}"
        for code in SUPPORTED_LANGUAGES:
            missing: list[str] = []
            for group in protocol.groups:
                if self._lookup_path(code, own_bundle, ("groups", group)) is None and (
                    self._lookup_path(code, "clinical", ("groups", group)) is None
                ):
                    missing.append(f"groups.{group}")
            for f in protocol.fields.values():
                bundle = f"protocol.{family_for_field(f.id)}"
                base = ("fields", f.id)
                for required_key in ("voice_prompt", "touch_label"):
                    if self._lookup_path(code, bundle, (*base, required_key)) is None:
                        missing.append(f"fields.{f.id}.{required_key}")
                if f.confirm_back and self._lookup_path(code, bundle, (*base, "confirm")) is None:
                    missing.append(f"fields.{f.id}.confirm")
                for opt in f.options:
                    has_field_label = (
                        self._lookup_path(code, bundle, (*base, "options", opt.value)) is not None
                    )
                    has_shared = (
                        self._lookup_path(code, "clinical", ("options", opt.value)) is not None
                    )
                    if not (has_field_label or has_shared):
                        missing.append(f"fields.{f.id}.options.{opt.value}")
                for unit in f.validation.units:
                    if self._lookup_path(code, "clinical", ("units", unit)) is None:
                        missing.append(f"units.{unit}")
            if missing:
                gaps[code] = sorted(set(missing))
        return gaps

    def assert_complete(self, protocols: tuple[Protocol, ...]) -> None:
        """Fail loudly if any protocol field lacks any supported language.

        Called at API startup and by a test, so an untranslated question cannot
        reach a patient — the kiosk would otherwise silently fall back to English
        for someone who selected Tamil.
        """
        all_gaps: dict[str, dict[str, list[str]]] = {}
        for protocol in protocols:
            gaps = self.missing_translations(protocol)
            if gaps:
                all_gaps[protocol.key] = gaps
        if all_gaps:
            summary = "; ".join(
                f"{proto}: " + ", ".join(f"{lang} ({len(keys)} keys)" for lang, keys in gaps.items())
                for proto, gaps in all_gaps.items()
            )
            raise LocalizationError(f"incomplete translations — {summary}")

    def missing_ui_keys(self) -> dict[str, list[str]]:
        """Keys present in the English chrome bundles but absent elsewhere."""
        gaps: dict[str, list[str]] = {}
        for bundle in ("kiosk", "errors", "consent", "clinical"):
            english = _flatten(self._bundles.get((DEFAULT_LANGUAGE, bundle), {}))
            for code in SUPPORTED_LANGUAGES:
                if code == DEFAULT_LANGUAGE:
                    continue
                translated = _flatten(self._bundles.get((code, bundle), {}))
                missing = sorted(set(english) - set(translated))
                if missing:
                    gaps.setdefault(code, []).extend(f"{bundle}:{k}" for k in missing)
        return gaps


def _interpolate(template: str, params: Mapping[str, Any]) -> str:
    """``{name}``-style interpolation that tolerates missing params.

    A KeyError here would be a blank screen for a patient mid-interview; leaving
    the placeholder visible is a far better failure.
    """
    if not params:
        return template
    out = template
    for key, value in params.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten(data: Mapping[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        # '$comment' entries are authoring notes for reviewers, not translatable
        # content — they must not be reported as translation gaps.
        if key.startswith("$"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys |= _flatten(value, path)
        else:
            keys.add(path)
    return keys
