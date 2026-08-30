# =============================================================================
# MediKiosk authorization policy (CLAUDE.md §5.1, §5.3, §29)
#
# Deny by default. Every rule below is an explicit grant, and every grant
# re-checks tenant equality even though PostgreSQL RLS will check it again —
# "no layer trusts the layer above it" (§5.1).
#
# Frontend route-hiding is never a control [RED LINE §5.1]; this file and RLS are.
# =============================================================================
package medikiosk.authz

import rego.v1

default allow := false

# -----------------------------------------------------------------------------
# Shared predicates
# -----------------------------------------------------------------------------

same_tenant if input.user.tenant_id == input.resource.tenant_id

# A staff member is scoped to the department they are assigned to (§5.2).
same_department if input.user.assigned_department_id == input.resource.department_id

is_clinician if input.user.role in {"physician", "ayush_practitioner"}

# A session that has been exported is sealed: no further writes (§21 [RED LINE]).
not_exported if input.resource.status != "exported"

# =============================================================================
# PATIENT TIER — ephemeral kiosk token, own session only (§4, §5.2)
# =============================================================================

allow if {
	input.user.role == "patient"
	input.action in {
		"read", "answer", "confirm", "upload_document",
		"grant_consent", "revoke_consent", "issue_upload_token",
	}
	input.resource.type in {"session", "consent", "document", "upload_token"}
	same_tenant
	# The token's patient must be the resource's patient. There is no path by
	# which a patient token reaches another patient's row.
	input.user.patient_id == input.resource.patient_id
	not_exported
}

# =============================================================================
# CAREGIVER RESPONDENT — only sessions in authorized_session_ids (§5.3, §6)
# A caregiver is a respondent. Consent-granting is deliberately absent here and
# is decided by authority_basis in the consent module [RED LINE §6].
# =============================================================================

allow if {
	input.user.role == "caregiver_respondent"
	input.action in {"read", "answer", "upload_document", "issue_upload_token"}
	input.resource.type in {"session", "document", "upload_token"}
	same_tenant
	input.resource.session_id in input.user.authorized_session_ids
	not_exported
}

# =============================================================================
# NURSE / TRIAGE — red-flag queue, own department only (§4, §5.2)
# =============================================================================

allow if {
	input.user.role == "nurse"
	input.action in {"read", "acknowledge", "escalate", "resolve"}
	input.resource.type == "red_flag_alert"
	same_tenant
	same_department
}

allow if {
	input.user.role == "nurse"
	input.action == "read"
	input.resource.type == "session"
	same_tenant
	same_department
}

# Staff-assisted capture is the mandatory no-phone fallback (§9, §17.1).
allow if {
	input.user.role == "nurse"
	input.action == "upload_document"
	input.resource.type == "document"
	same_tenant
	same_department
	not_exported
}

allow if {
	input.user.role == "nurse"
	input.action in {"read", "verify"}
	input.resource.type == "extraction_candidate"
	same_tenant
	same_department
}

# =============================================================================
# PHYSICIAN / AYUSH PRACTITIONER — assigned sessions, own department (§5.2)
# =============================================================================

allow if {
	is_clinician
	input.action == "read"
	input.resource.type in {
		"session", "clinical_fact", "summary", "document",
		"timeline", "red_flag_alert", "physician_review",
		"fact_conflict", "extraction_candidate",
	}
	same_tenant
	same_department
}

# Write authority: edit, reject, request clarification, approve. Every one is
# gated on the session not already being exported.
allow if {
	is_clinician
	input.action in {
		"open_review", "edit_fact", "reject", "request_clarification",
		"approve", "resolve_conflict", "verify",
	}
	input.resource.type in {
		"physician_review", "clinical_fact", "summary",
		"fact_conflict", "extraction_candidate",
	}
	same_tenant
	same_department
	not_exported
}

# NAMASTE / ICD-11 TM2 confirmation is a practitioner act (§24).
allow if {
	is_clinician
	input.action in {"suggest_coding", "confirm_coding"}
	input.resource.type in {"clinical_fact", "namaste_mapping"}
	same_tenant
	same_department
	not_exported
}

# =============================================================================
# CLINICAL ADMIN (Governance) — review queue for clinical CONTENT only.
# No clinical record access: governance changes route through the CI gate (§5.2).
# =============================================================================

allow if {
	input.user.role == "clinical_admin"
	input.action == "read"
	input.resource.type in {
		"protocol_version", "red_flag_ruleset",
		"governance_queue", "clinical_metrics",
	}
	same_tenant
}

# =============================================================================
# IT ADMIN — own-tenant configuration only, never clinical data (§5.2)
# =============================================================================

allow if {
	input.user.role == "it_admin"
	input.action in {"read", "write"}
	input.resource.type in {
		"tenant", "department", "device", "app_user",
		"integration_config", "tenant_protocol_config",
	}
	same_tenant
	# Privileged configuration writes require a step-up MFA assertion (§4).
	step_up_ok
}

step_up_ok if input.action == "read"

step_up_ok if {
	input.action == "write"
	input.user.mfa_satisfied
}

# Integration delivery status is operational, not clinical.
allow if {
	input.user.role == "it_admin"
	input.action == "read"
	input.resource.type in {"integration_delivery", "outbox_event"}
	same_tenant
}

# =============================================================================
# SECURITY / PRIVACY OFFICER — audit, consent and retention posture.
# Audit export forces step-up MFA (§4, §5.2). No clinical editing, ever.
# =============================================================================

allow if {
	input.user.role == "security_officer"
	input.action == "read"
	input.resource.type in {"consent_status", "retention_status", "audit_event"}
	same_tenant
}

allow if {
	input.user.role == "security_officer"
	input.action == "export"
	input.resource.type == "audit_event"
	same_tenant
	input.user.mfa_satisfied
}

allow if {
	input.user.role == "security_officer"
	input.action == "verify"
	input.resource.type == "audit_chain"
	same_tenant
}

# =============================================================================
# Explanation surface — used by decision logs and the 403 reason code. It never
# widens access; it only describes why a request was refused.
# =============================================================================

reason := "allow" if allow

reason := "tenant_mismatch" if {
	not allow
	input.user.tenant_id != input.resource.tenant_id
}

reason := "department_mismatch" if {
	not allow
	input.user.tenant_id == input.resource.tenant_id
	input.resource.department_id
	input.user.assigned_department_id != input.resource.department_id
}

reason := "session_sealed_after_export" if {
	not allow
	input.resource.status == "exported"
}

reason := "role_not_permitted" if {
	not allow
	input.user.tenant_id == input.resource.tenant_id
	not input.resource.status == "exported"
	not department_conflict
}

department_conflict if {
	input.resource.department_id
	input.user.assigned_department_id != input.resource.department_id
}
