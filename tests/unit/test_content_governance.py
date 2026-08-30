"""Governed content invariants (CLAUDE.md §10, §12, §14, §46, §52).

These tests are the automated half of the clinical-governance gate. They do not
judge clinical correctness — that is the Board's and the
``clinical-safety-reviewer`` agent's job (§59). They enforce the *structural*
guarantees that make the clinical review meaningful:

* protocol content carries no patient-facing text, so translation review and
  clinical review are genuinely separate;
* the checksum a running system records is the checksum of the file on disk;
* every red-flag rule is armed, i.e. its inputs exist in some loaded protocol;
* every patient-facing string exists in all five supported languages, so a
  patient who chose Tamil never silently receives English.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from medikiosk.modules.clinical_protocol.model import ProtocolContentError
from medikiosk.modules.clinical_protocol.registry import (
    KNOWN_FAMILIES,
    ProtocolRegistry,
)
from medikiosk.modules.localization.registry import (
    LANGUAGES,
    SUPPORTED_LANGUAGES,
    LocalizationRegistry,
)

pytestmark = pytest.mark.unit


class TestProtocolContentLoads:
    def test_both_families_load(self, protocol_registry: ProtocolRegistry):
        descriptors = {d.family for d in protocol_registry.describe()}
        assert descriptors == KNOWN_FAMILIES

    def test_general_medicine_covers_the_required_history(self, general_medicine):
        """§11: chief complaint + SOCRATES + ROS + PMH/PSH + drug/allergy +
        family + personal history, plus procedure history as a first-class
        category (§13)."""
        groups = set(general_medicine.groups)
        assert {
            "chief_complaint",
            "hpi",
            "review_of_systems",
            "past_medical_history",
            "past_surgical_history",
            "procedure_history",
            "medications",
            "allergies",
            "family_history",
            "personal_history",
            "ample_fast_path",
        } <= groups

    def test_socrates_is_fully_represented(self, general_medicine):
        """All eight SOCRATES dimensions must have a field."""
        concepts = {f.concept_code for f in general_medicine.fields.values()}
        socrates = {
            "symptom_site",            # Site
            "symptom_onset",           # Onset
            "symptom_character",       # Character
            "symptom_radiation",       # Radiation
            "symptom_association",     # Associations
            "symptom_time_course",     # Time course
            "symptom_exacerbating",    # Exacerbating/relieving
            "symptom_relieving",
            "symptom_severity",        # Severity
        }
        assert socrates <= concepts

    def test_no_competing_symptom_framework(self, general_medicine):
        """§11 [DECISION]: SOCRATES is the sole framework.

        OPQRST/OLDCARTS/PQRST must not appear as separate field sets, which
        would ask the same question twice under different taxonomies.
        """
        text = json.dumps(
            [f.id for f in general_medicine.fields.values()]
            + [f.concept_code for f in general_medicine.fields.values()]
        ).lower()
        for banned in ("opqrst", "oldcarts", "pqrst"):
            assert banned not in text

    def test_ample_is_exactly_five_fields(self, general_medicine):
        """§14.2: the fast path switches to a fixed 5-field AMPLE set."""
        assert len(general_medicine.ample_fields) == 5
        concepts = {general_medicine.fields[f].concept_code for f in general_medicine.ample_fields}
        assert concepts == {
            "ample_allergies",
            "ample_medications",
            "ample_past_history",
            "ample_last_oral_intake",
            "ample_events",
        }

    def test_ample_fields_are_not_routinely_required(self, general_medicine):
        """§11: AMPLE is used ONLY in the red-flag fast path."""
        for fid in general_medicine.ample_fields:
            assert general_medicine.fields[fid].required is False

    def test_procedure_history_is_a_first_class_category(self, general_medicine):
        categories = {f.category for f in general_medicine.fields.values()}
        assert "procedure_history" in categories


class TestAyushComposition:
    def test_ayurveda_extends_general_medicine(self, ayurveda, general_medicine):
        """§12: same engine, extended C and F — not a second system."""
        gm_ids = set(general_medicine.fields)
        ay_ids = set(ayurveda.fields)
        assert gm_ids <= ay_ids, "AYUSH must inherit the whole general history"
        assert ay_ids - gm_ids, "AYUSH must add its own fields"

    def test_dashavidha_pariksha_all_ten_parameters(self, ayurveda):
        concepts = {f.concept_code for f in ayurveda.fields.values()}
        dashavidha = {
            "prakriti_indicator",
            "vikriti_indicator",
            "sara_indicator",
            "samhanana_indicator",
            "pramana_measure",
            "satmya_indicator",
            "sattva_indicator",
            "ahara_shakti",
            "vyayama_shakti",
            "vaya_stage",
        }
        assert dashavidha <= concepts, f"missing: {dashavidha - concepts}"

    def test_ahara_vihara_nidana_samprapti_present(self, ayurveda):
        categories = {f.category for f in ayurveda.fields.values()}
        assert {"ahara_vihara", "nidana", "samprapti"} <= categories

    def test_ayurveda_inherits_the_same_ample_fast_path(self, ayurveda):
        assert all(fid.startswith("gm.ample.") for fid in ayurveda.ample_fields)

    def test_inherited_field_definitions_are_identical(self, ayurveda, general_medicine):
        """Byte-identical inherited fields are what makes 'one engine' true."""
        for fid, gm_field in general_medicine.fields.items():
            assert ayurveda.fields[fid] == gm_field

    def test_composition_checksum_covers_the_base(self, ayurveda, general_medicine):
        """Editing the base must change the derived protocol's checksum."""
        assert ayurveda.content_checksum != general_medicine.content_checksum

    def test_self_extension_rejected(self, tmp_path):
        (tmp_path / "protocols" / "general_medicine").mkdir(parents=True)
        (tmp_path / "protocols" / "general_medicine" / "loop.json").write_text(
            json.dumps({
                "protocol_family": "general_medicine",
                "version": "loop",
                "extends": {"family": "general_medicine", "version": "loop"},
                "concepts": [], "fields": [],
            })
        )
        with pytest.raises(ProtocolContentError, match="extends itself"):
            ProtocolRegistry(tmp_path).load("general_medicine", "loop")

    def test_missing_base_rejected(self, tmp_path):
        (tmp_path / "protocols" / "ayush_ayurveda").mkdir(parents=True)
        (tmp_path / "protocols" / "ayush_ayurveda" / "orphan.json").write_text(
            json.dumps({
                "protocol_family": "ayush_ayurveda",
                "version": "orphan",
                "extends": {"family": "general_medicine", "version": "nope"},
                "concepts": [], "fields": [],
            })
        )
        with pytest.raises(ProtocolContentError, match="extends missing base"):
            ProtocolRegistry(tmp_path).load("ayush_ayurveda", "orphan")


class TestContentIsLanguageNeutral:
    @pytest.mark.parametrize("family,version", [
        ("general_medicine", "v1"),
        ("ayush_ayurveda", "v1"),
    ])
    def test_no_patient_facing_text_in_protocol_content(self, content_root, family, version):
        raw = (content_root / "protocols" / family / f"{version}.json").read_text("utf-8")
        document = json.loads(raw)
        for field in document.get("fields", []):
            for banned in ("label", "voice_prompt", "touch_label", "help", "prompt", "text"):
                assert banned not in field, (
                    f"{field.get('id')} embeds display text {banned!r}; it belongs in i18n"
                )

    def test_loader_rejects_embedded_display_text(self, tmp_path):
        (tmp_path / "protocols" / "general_medicine").mkdir(parents=True)
        (tmp_path / "protocols" / "general_medicine" / "bad.json").write_text(
            json.dumps({
                "protocol_family": "general_medicine",
                "version": "bad",
                "concepts": [{"code": "c", "category": "symptom"}],
                "fields": [{
                    "id": "gm.bad.a", "concept_code": "c", "category": "symptom",
                    "group": "g", "order": 1, "value_type": "boolean", "widget": "yes_no",
                    "ample": True,
                    "voice_prompt": "What is wrong?",
                }],
            }, ensure_ascii=False)
        )
        with pytest.raises(ProtocolContentError, match="must not live in protocol content"):
            ProtocolRegistry(tmp_path).load("general_medicine", "bad")

    def test_field_ids_use_registered_prefixes(self, all_protocols):
        for protocol in all_protocols:
            for fid in protocol.fields:
                assert fid.startswith(("gm.", "ay.")), fid


class TestChecksumIntegrity:
    def test_checksum_matches_the_file_on_disk(self, content_root, general_medicine):
        raw = (content_root / "protocols" / "general_medicine" / "v1.json").read_bytes()
        assert general_medicine.content_checksum == hashlib.sha256(raw).hexdigest()

    def test_ruleset_checksum_matches_the_file_on_disk(self, content_root, emergency_ruleset):
        raw = (content_root / "redflag" / "emergency_v1.json").read_bytes()
        assert emergency_ruleset.content_checksum == hashlib.sha256(raw).hexdigest()


class TestRedFlagContentGovernance:
    def test_no_rule_is_disarmed(self, red_flag_registry, emergency_ruleset, all_protocols):
        """A rule whose inputs no protocol defines can never fire (§14, §52)."""
        broken = red_flag_registry.validate_against(emergency_ruleset, all_protocols)
        assert broken == (), f"disarmed or mis-declared rules: {broken}"

    def test_every_rule_reads_at_least_one_field(self, emergency_ruleset):
        for rule in emergency_ruleset.rules:
            assert rule.input_fields, f"{rule.id} would fire unconditionally"

    def test_rules_carry_no_patient_facing_text(self, content_root):
        """§14: the kiosk shows a calm localized screen, never rule text."""
        document = json.loads((content_root / "redflag" / "emergency_v1.json").read_text("utf-8"))
        for rule in document["rules"]:
            for banned in ("escalationMessage", "escalation_message", "patient_message"):
                assert banned not in rule, f"{rule['id']} embeds patient-facing text"

    def test_every_rule_has_a_clinician_rationale(self, emergency_ruleset):
        for rule in emergency_ruleset.rules:
            assert len(rule.staff_rationale) > 30, (
                f"{rule.id} rationale is too thin for a nurse to act on"
            )

    def test_critical_rules_have_the_tightest_sla(self, emergency_ruleset):
        for rule in emergency_ruleset.rules:
            if rule.severity == "critical":
                assert rule.sla_seconds <= 300, f"{rule.id} SLA is too slow for critical"

    def test_red_flag_input_fields_are_marked_in_protocol_content(
        self, emergency_ruleset, all_protocols
    ):
        """Marking the dependency in both places is what lets the CI gate catch
        a protocol change that would disarm a safety rule."""
        rule_inputs: set[str] = set()
        for rule in emergency_ruleset.rules:
            rule_inputs.update(rule.input_fields)

        unmarked: list[str] = []
        for protocol in all_protocols:
            for fid in rule_inputs & set(protocol.fields):
                if not protocol.fields[fid].red_flag_input:
                    unmarked.append(fid)
        assert not unmarked, f"fields read by red-flag rules but not marked: {sorted(set(unmarked))}"

    def test_duplicate_rule_id_rejected(self, tmp_path):
        (tmp_path / "redflag").mkdir(parents=True)
        rule = {
            "id": "dup", "name": "n", "severity": "high", "sla_seconds": 60,
            "category": "c", "staff_rationale": "x" * 40,
            "predicate": {"op": "answered", "field": "gm.a"},
            "input_fields": ["gm.a"],
        }
        (tmp_path / "redflag" / "dup.json").write_text(
            json.dumps({"version": "dup", "rules": [rule, rule]})
        )
        from medikiosk.modules.triage.red_flag_engine import RedFlagRegistry

        with pytest.raises(ProtocolContentError, match="duplicate red-flag rule id"):
            RedFlagRegistry(tmp_path).load("dup")

    def test_rule_without_sla_rejected(self, tmp_path):
        (tmp_path / "redflag").mkdir(parents=True)
        (tmp_path / "redflag" / "nosla.json").write_text(json.dumps({
            "version": "nosla",
            "rules": [{
                "id": "r", "name": "n", "severity": "high", "sla_seconds": 0,
                "category": "c", "staff_rationale": "x" * 40,
                "predicate": {"op": "answered", "field": "gm.a"},
                "input_fields": ["gm.a"],
            }],
        }))
        from medikiosk.modules.triage.red_flag_engine import RedFlagRegistry

        with pytest.raises(ProtocolContentError, match="positive SLA"):
            RedFlagRegistry(tmp_path).load("nosla")


class TestLocalizationCompleteness:
    def test_exactly_five_supported_languages(self):
        assert SUPPORTED_LANGUAGES == ("en", "hi", "ta", "te", "ml")

    def test_every_language_declares_asr_and_tts_resources(self):
        """Adding a language must be a resource change, not a code change."""
        for profile in LANGUAGES:
            assert profile.asr_locale, profile.code
            assert profile.tts_locale, profile.code
            assert profile.tts_voice, profile.code
            assert profile.endonym, profile.code

    def test_endonyms_are_in_their_own_script(self):
        """A patient who cannot read English must recognise their language."""
        expected = {
            "hi": "हिन्दी",
            "ta": "தமிழ்",
            "te": "తెలుగు",
            "ml": "മലയാളം",
        }
        by_code = {p.code: p.endonym for p in LANGUAGES}
        for code, endonym in expected.items():
            assert by_code[code] == endonym

    def test_no_protocol_field_is_missing_a_translation(
        self, localization: LocalizationRegistry, all_protocols
    ):
        """Fails the build if any question lacks any of the five languages."""
        localization.assert_complete(all_protocols)

    def test_ui_chrome_bundles_have_no_gaps(self, localization: LocalizationRegistry):
        gaps = localization.missing_ui_keys()
        assert gaps == {}, f"untranslated chrome keys: {gaps}"

    @pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
    def test_every_field_renders_without_english_fallback(
        self, localization: LocalizationRegistry, all_protocols, language
    ):
        fell_back: list[str] = []
        for protocol in all_protocols:
            for field in protocol.fields.values():
                rendered = localization.render_field(protocol, field, language)
                assert rendered.voice_prompt
                assert rendered.touch_label
                assert len(rendered.options) == len(field.options)
                if rendered.fallback_used and language != "en":
                    fell_back.append(f"{protocol.key}:{field.id}")
        assert not fell_back, f"English fallback used for: {sorted(set(fell_back))}"

    def test_inherited_fields_reuse_the_general_medicine_bundle(
        self, localization: LocalizationRegistry, ayurveda, general_medicine
    ):
        """SOCRATES is translated once, and an AYUSH session uses that one copy."""
        for language in SUPPORTED_LANGUAGES:
            in_ayush = localization.render_field(
                ayurveda, ayurveda.fields["gm.hpi.severity"], language
            )
            in_gm = localization.render_field(
                general_medicine, general_medicine.fields["gm.hpi.severity"], language
            )
            assert in_ayush.voice_prompt == in_gm.voice_prompt

    def test_language_normalisation(self, localization: LocalizationRegistry):
        assert localization.normalize("ta-IN") == "ta"
        assert localization.normalize("HI") == "hi"
        assert localization.normalize("ml_IN") == "ml"
        assert localization.normalize("fr") == "en", "unsupported falls back to English"
        assert localization.normalize(None) == "en"
        assert localization.normalize("") == "en"

    def test_error_reason_codes_are_all_translated(self, localization: LocalizationRegistry):
        """Every reason_code a patient can hit must have localized words (§37)."""
        from medikiosk import errors as error_module

        codes = {
            cls.reason_code
            for cls in vars(error_module).values()
            if isinstance(cls, type)
            and issubclass(cls, error_module.MediKioskError)
            and cls is not error_module.MediKioskError
        }
        for language in SUPPORTED_LANGUAGES:
            bundle = localization.bundle(language, "errors")
            missing = [code for code in codes if code not in bundle]
            assert not missing, f"{language}: no message for reason codes {missing}"

    def test_consent_notice_version_is_recorded(self, localization: LocalizationRegistry):
        """§7.2: it must be provable which words a patient actually heard."""
        for language in SUPPORTED_LANGUAGES:
            bundle = localization.bundle(language, "consent")
            assert bundle.get("notice_version"), language

    def test_consent_has_an_audio_script_for_every_purpose(
        self, localization: LocalizationRegistry
    ):
        """§7.2: internal consent is audio-explained, not a wall of text."""
        for language in SUPPORTED_LANGUAGES:
            purposes = localization.bundle(language, "consent")["purposes"]
            for name, purpose in purposes.items():
                assert purpose.get("spoken"), f"{language}:{name} has no audio script"

    def test_exactly_one_consent_purpose_is_required(
        self, localization: LocalizationRegistry
    ):
        """Only staff access may be mandatory; the rest are genuinely optional."""
        purposes = localization.bundle("en", "consent")["purposes"]
        required = {name for name, p in purposes.items() if p.get("required")}
        assert required == {"staff_access"}

    def test_escalation_copy_is_calm_and_non_technical(
        self, localization: LocalizationRegistry
    ):
        """§14: the patient-facing escalation screen is never a technical alert."""
        banned = ("alert", "critical", "emergency", "red flag", "acs", "severity",
                  "triage", "rule", "diagnos")
        for language in SUPPORTED_LANGUAGES:
            escalation = localization.bundle(language, "kiosk")["escalation"]
            # '$'-prefixed entries are authoring notes for reviewers and are
            # never rendered; only patient-visible copy is under test.
            visible = {k: v for k, v in escalation.items() if not k.startswith("$")}
            text = json.dumps(visible, ensure_ascii=False).lower()
            for word in banned:
                assert word not in text, f"{language} escalation copy contains {word!r}"

    def test_unsupported_language_profile_raises(self, localization: LocalizationRegistry):
        from medikiosk.modules.localization.registry import LocalizationError

        with pytest.raises(LocalizationError):
            localization.profile("fr")
