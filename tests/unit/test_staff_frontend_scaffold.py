"""Phase 2 staff dashboard scaffolding checks.

These are deliberately static tests because the frontend's most important
properties are architectural: it must use the authorization-code + PKCE flow,
must not persist a bearer token, and must retain the server-side authorization
boundary rather than attempting role checks in the browser.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "staff-frontend"


def test_staff_workspace_is_a_pinned_next_application() -> None:
    package = json.loads((APP / "package.json").read_text("utf-8"))
    assert package["private"] is True
    # Explicit patched versions: no floating framework upgrades in a clinical UI.
    assert package["dependencies"]["next"] == "16.3.3"
    assert package["dependencies"]["react"] == "19.2.8"
    assert package["dependencies"]["react-dom"] == "19.2.8"


def test_staff_workspace_uses_pkce_and_never_persists_bearer_tokens() -> None:
    source = (APP / "app" / "staff-workspace.tsx").read_text("utf-8")
    assert 'response_type: "code"' in source
    assert 'code_challenge_method: "S256"' in source
    assert "code_verifier" in source
    assert "sessionStorage.setItem(verifierKey" in source
    assert "localStorage" not in source
    assert "innerHTML" not in source


def test_staff_workspace_relies_on_api_authorization_and_attestation() -> None:
    source = (APP / "app" / "staff-workspace.tsx").read_text("utf-8")
    assert 'headers.set("authorization", `Bearer ${accessToken}`)' in source
    assert '"/v1/reviews"' in source
    assert "/history" in source
    assert 'attestation: true' in source
    assert 'export_targets: ["fhir"]' in source
    # Access denial is deliberately handled as an API result, never assumed from UI state.
    assert "Access is enforced by the API" in source


def test_csp_uses_a_per_response_nonce() -> None:
    source = (APP / "proxy.ts").read_text("utf-8")
    assert "crypto.randomUUID" in source
    assert "'strict-dynamic'" in source
    assert "'unsafe-inline'" not in source
    assert 'requestHeaders.set("Content-Security-Policy", policy)' in source


def test_staff_page_is_forced_dynamic_for_nonce_based_csp() -> None:
    source = (APP / "app" / "page.tsx").read_text("utf-8")
    assert "await connection()" in source


def test_keycloak_realm_binds_the_configured_mfa_browser_flow() -> None:
    realm = json.loads((ROOT / "infra" / "keycloak" / "realm-medikiosk.json").read_text("utf-8"))
    aliases = {flow["alias"] for flow in realm["authenticationFlows"]}
    assert realm["browserFlow"] == "medikiosk-browser-mfa"
    assert realm["browserFlow"] in aliases
