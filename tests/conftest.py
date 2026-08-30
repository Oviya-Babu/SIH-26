"""Shared fixtures for every test tier."""

from __future__ import annotations

from pathlib import Path

import pytest

from medikiosk.modules.clinical_protocol.engine import Thresholds
from medikiosk.modules.clinical_protocol.model import (
    Concept,
    Field,
    Option,
    Protocol,
    Validation,
    ValueType,
    Widget,
    build_protocol,
)
from medikiosk.modules.clinical_protocol.registry import ProtocolRegistry
from medikiosk.modules.localization.registry import LocalizationRegistry
from medikiosk.modules.triage.red_flag_engine import RedFlagRegistry

CONTENT_ROOT = Path(__file__).resolve().parents[1] / "content"


@pytest.fixture(scope="session")
def content_root() -> Path:
    return CONTENT_ROOT


@pytest.fixture(scope="session")
def protocol_registry(content_root: Path) -> ProtocolRegistry:
    registry = ProtocolRegistry(content_root)
    registry.load_all()
    return registry


@pytest.fixture(scope="session")
def general_medicine(protocol_registry: ProtocolRegistry) -> Protocol:
    return protocol_registry.load("general_medicine", "v1")


@pytest.fixture(scope="session")
def ayurveda(protocol_registry: ProtocolRegistry) -> Protocol:
    return protocol_registry.load("ayush_ayurveda", "v1")


@pytest.fixture(scope="session")
def all_protocols(general_medicine: Protocol, ayurveda: Protocol) -> tuple[Protocol, ...]:
    return (general_medicine, ayurveda)


@pytest.fixture(scope="session")
def localization(content_root: Path) -> LocalizationRegistry:
    registry = LocalizationRegistry(content_root)
    registry.load_all()
    return registry


@pytest.fixture(scope="session")
def red_flag_registry(content_root: Path) -> RedFlagRegistry:
    return RedFlagRegistry(content_root)


@pytest.fixture(scope="session")
def emergency_ruleset(red_flag_registry: RedFlagRegistry):
    return red_flag_registry.load("emergency_v1")


@pytest.fixture
def thresholds() -> Thresholds:
    return Thresholds()


# ---------------------------------------------------------------------------
# A tiny synthetic protocol, for engine tests that must exercise a specific
# branch shape without depending on governed clinical content.
# ---------------------------------------------------------------------------
@pytest.fixture
def toy_protocol() -> Protocol:
    concepts = [
        Concept(code="c_root", category="chief_complaint"),
        Concept(code="c_branch", category="symptom"),
        Concept(code="c_scale", category="symptom"),
        Concept(code="c_optional", category="symptom"),
        Concept(code="c_ample", category="ample_field"),
    ]
    fields = [
        Field(
            id="gm.toy.root",
            concept_code="c_root",
            category="chief_complaint",
            group="g1",
            order=10,
            required=True,
            value_type=ValueType.SINGLE_SELECT,
            widget=Widget.CHOICE_GRID,
            options=(Option(value="pain"), Option(value="fever")),
        ),
        Field(
            id="gm.toy.branch",
            concept_code="c_branch",
            category="symptom",
            group="g1",
            order=20,
            required=True,
            value_type=ValueType.BOOLEAN,
            widget=Widget.YES_NO,
            depends_on={"op": "equals", "field": "gm.toy.root", "value": "pain"},
        ),
        Field(
            id="gm.toy.scale",
            concept_code="c_scale",
            category="symptom",
            group="g2",
            order=30,
            required=True,
            value_type=ValueType.SCALE,
            widget=Widget.SEVERITY_FACES,
            validation=Validation(min=0, max=10),
            confirm_back=True,
        ),
        Field(
            id="gm.toy.optional",
            concept_code="c_optional",
            category="symptom",
            group="g2",
            order=40,
            required=False,
            value_type=ValueType.TEXT,
            widget=Widget.SHORT_TEXT,
            validation=Validation(max_length=50),
        ),
        Field(
            id="gm.toy.ample",
            concept_code="c_ample",
            category="ample_field",
            group="g3",
            order=50,
            required=False,
            ample=True,
            value_type=ValueType.BOOLEAN,
            widget=Widget.YES_NO,
        ),
    ]
    return build_protocol(
        family="general_medicine",
        version="toy",
        content_checksum="toy",
        concepts=concepts,
        fields=fields,
    )
