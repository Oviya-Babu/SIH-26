"""RBAC — "does this role even have this endpoint?" (CLAUDE.md §5.1, §5.2).

This is the coarse, static gate that runs *before* OPA. It exists because OPA
answers a resource question ("this session, in this department") and something
must first answer the cheaper, unconditional one ("a nurse has no business
calling an admin endpoint at all").

Deny by default: an endpoint whose capability is not listed for a role is
refused, and a capability that appears in no role's set is unreachable.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    # --- kiosk / patient tier -------------------------------------------------
    SESSION_START = "session.start"
    SESSION_ANSWER = "session.answer"
    SESSION_READ_OWN = "session.read_own"
    SESSION_CONFIRM = "session.confirm"
    CONSENT_GRANT = "consent.grant"
    CONSENT_REVOKE = "consent.revoke"
    CAREGIVER_ACKNOWLEDGE = "caregiver.acknowledge"
    DOCUMENT_UPLOAD = "document.upload"
    UPLOAD_TOKEN_ISSUE = "upload_token.issue"

    # --- nurse ----------------------------------------------------------------
    TRIAGE_QUEUE_READ = "triage.queue_read"
    TRIAGE_ALERT_ACK = "triage.alert_ack"
    TRIAGE_ALERT_ESCALATE = "triage.alert_escalate"
    TRIAGE_ALERT_RESOLVE = "triage.alert_resolve"
    SESSION_READ_DEPARTMENT = "session.read_department"
    STAFF_ASSISTED_CAPTURE = "document.staff_capture"
    EXTRACTION_VERIFY = "extraction.verify"

    # --- physician / AYUSH practitioner ---------------------------------------
    REVIEW_QUEUE_READ = "review.queue_read"
    REVIEW_OPEN = "review.open"
    REVIEW_EDIT_FACT = "review.edit_fact"
    REVIEW_REJECT = "review.reject"
    REVIEW_REQUEST_CLARIFICATION = "review.request_clarification"
    REVIEW_APPROVE = "review.approve"
    CONFLICT_RESOLVE = "conflict.resolve"
    NAMASTE_SUGGEST = "namaste.suggest"
    NAMASTE_CONFIRM = "namaste.confirm"
    CLINICAL_READ = "clinical.read"
    DOCUMENT_READ = "document.read"

    # --- clinical admin (governance) -----------------------------------------
    GOVERNANCE_QUEUE_READ = "governance.queue_read"
    GOVERNANCE_PROTOCOL_READ = "governance.protocol_read"
    GOVERNANCE_REDFLAG_READ = "governance.redflag_read"
    GOVERNANCE_METRICS_READ = "governance.metrics_read"

    # --- IT admin -------------------------------------------------------------
    TENANT_CONFIG_READ = "tenant.config_read"
    TENANT_CONFIG_WRITE = "tenant.config_write"
    DEVICE_MANAGE = "device.manage"
    USER_MANAGE = "user.manage"
    INTEGRATION_CONFIG = "integration.config"
    INTEGRATION_STATUS_READ = "integration.status_read"

    # --- security / privacy officer ------------------------------------------
    AUDIT_EXPORT = "audit.export"
    AUDIT_VERIFY = "audit.verify"
    CONSENT_STATUS_READ = "consent.status_read"
    RETENTION_STATUS_READ = "retention.status_read"


_PATIENT: frozenset[Capability] = frozenset(
    {
        Capability.SESSION_START,
        Capability.SESSION_ANSWER,
        Capability.SESSION_READ_OWN,
        Capability.SESSION_CONFIRM,
        Capability.CONSENT_GRANT,
        Capability.CONSENT_REVOKE,
        Capability.CAREGIVER_ACKNOWLEDGE,
        Capability.DOCUMENT_UPLOAD,
        Capability.UPLOAD_TOKEN_ISSUE,
    }
)

# A caregiver respondent gets the same *actions* as a patient, minus the ability
# to grant consent. Consent authority is decided separately, per authorization
# basis, by the consent module (§6) — never by role membership alone.
_CAREGIVER: frozenset[Capability] = frozenset(_PATIENT - {Capability.CONSENT_GRANT})

_NURSE: frozenset[Capability] = frozenset(
    {
        Capability.TRIAGE_QUEUE_READ,
        Capability.TRIAGE_ALERT_ACK,
        Capability.TRIAGE_ALERT_ESCALATE,
        Capability.TRIAGE_ALERT_RESOLVE,
        Capability.SESSION_READ_DEPARTMENT,
        Capability.STAFF_ASSISTED_CAPTURE,
        Capability.EXTRACTION_VERIFY,
    }
)

_PHYSICIAN: frozenset[Capability] = frozenset(
    {
        Capability.REVIEW_QUEUE_READ,
        Capability.REVIEW_OPEN,
        Capability.REVIEW_EDIT_FACT,
        Capability.REVIEW_REJECT,
        Capability.REVIEW_REQUEST_CLARIFICATION,
        Capability.REVIEW_APPROVE,
        Capability.CONFLICT_RESOLVE,
        Capability.CLINICAL_READ,
        Capability.DOCUMENT_READ,
        Capability.SESSION_READ_DEPARTMENT,
        Capability.TRIAGE_QUEUE_READ,
        Capability.EXTRACTION_VERIFY,
    }
)

# Identical clinical authority; NAMASTE/ICD-11 TM2 confirmation is the AYUSH
# practitioner's own act (§12, §24). A general physician also gets it so a
# general-medicine department using dual coding is not blocked.
_AYUSH: frozenset[Capability] = frozenset(
    _PHYSICIAN | {Capability.NAMASTE_SUGGEST, Capability.NAMASTE_CONFIRM}
)

_CLINICAL_ADMIN: frozenset[Capability] = frozenset(
    {
        Capability.GOVERNANCE_QUEUE_READ,
        Capability.GOVERNANCE_PROTOCOL_READ,
        Capability.GOVERNANCE_REDFLAG_READ,
        Capability.GOVERNANCE_METRICS_READ,
    }
)

_IT_ADMIN: frozenset[Capability] = frozenset(
    {
        Capability.TENANT_CONFIG_READ,
        Capability.TENANT_CONFIG_WRITE,
        Capability.DEVICE_MANAGE,
        Capability.USER_MANAGE,
        Capability.INTEGRATION_CONFIG,
        Capability.INTEGRATION_STATUS_READ,
    }
)

_SECURITY_OFFICER: frozenset[Capability] = frozenset(
    {
        Capability.AUDIT_EXPORT,
        Capability.AUDIT_VERIFY,
        Capability.CONSENT_STATUS_READ,
        Capability.RETENTION_STATUS_READ,
    }
)

ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "patient": _PATIENT,
    "caregiver_respondent": _CAREGIVER,
    "nurse": _NURSE,
    "physician": _PHYSICIAN,
    "ayush_practitioner": _AYUSH,
    "clinical_admin": _CLINICAL_ADMIN,
    "it_admin": _IT_ADMIN,
    "security_officer": _SECURITY_OFFICER,
}

# Capabilities that additionally require a step-up MFA assertion on the token,
# not merely an MFA-enrolled account (§4 Admin/Security row).
STEP_UP_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.AUDIT_EXPORT,
        Capability.USER_MANAGE,
        Capability.INTEGRATION_CONFIG,
    }
)


def has_capability(role: str, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def capabilities_for(role: str) -> frozenset[Capability]:
    return ROLE_CAPABILITIES.get(role, frozenset())


def assert_no_privilege_overlap() -> None:
    """Least-privilege invariant, asserted by a test (§5, §51).

    A nurse must never hold a physician-authority capability, and no clinical
    role may hold an admin or security capability. Enforcing this as an assertion
    means a future "just add the capability" edit fails CI.
    """
    physician_only = {
        Capability.REVIEW_APPROVE,
        Capability.REVIEW_EDIT_FACT,
        Capability.REVIEW_REJECT,
        Capability.CONFLICT_RESOLVE,
    }
    if _NURSE & physician_only:
        raise AssertionError("nurse holds physician-authority capabilities (§5.2)")

    admin_and_security = _IT_ADMIN | _SECURITY_OFFICER
    for role in ("nurse", "physician", "ayush_practitioner", "patient", "caregiver_respondent"):
        overlap = ROLE_CAPABILITIES[role] & admin_and_security
        if overlap:
            raise AssertionError(f"{role} holds admin/security capabilities: {overlap}")

    # A Security/Privacy Officer must not be able to edit clinical records (§5.2).
    clinical_write = {
        Capability.REVIEW_EDIT_FACT,
        Capability.REVIEW_APPROVE,
        Capability.SESSION_ANSWER,
        Capability.CONFLICT_RESOLVE,
        Capability.NAMASTE_CONFIRM,
    }
    if _SECURITY_OFFICER & clinical_write:
        raise AssertionError("security_officer holds clinical write capabilities (§5.2)")

    # Clinical governance must not have a direct production clinical write; its
    # changes route through the CI governance gate (§5.2, §46).
    if _CLINICAL_ADMIN & (clinical_write | {Capability.CLINICAL_READ}):
        raise AssertionError("clinical_admin holds direct clinical access (§5.2)")
