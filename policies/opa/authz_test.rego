# =============================================================================
# Rego policy tests (CLAUDE.md §5.3, §51, §64.8).
#
# The three unauthorized-access checks §5.3 names explicitly are the headline
# cases here, plus the deny-by-default property that everything else rests on:
#
#   nurse    → admin endpoint       → deny
#   patient  → another patient      → deny
#   physician→ wrong department     → deny
#
#   opa test policies/opa
# =============================================================================
package medikiosk.authz_test

import rego.v1

import data.medikiosk.authz

TENANT_A := "11111111-1111-1111-1111-111111111111"

TENANT_B := "22222222-2222-2222-2222-222222222222"

DEPT_MED := "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

DEPT_AYUSH := "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

PATIENT_1 := "cccccccc-cccc-cccc-cccc-cccccccccccc"

PATIENT_2 := "dddddddd-dddd-dddd-dddd-dddddddddddd"

SESSION_1 := "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

SESSION_2 := "ffffffff-ffff-ffff-ffff-ffffffffffff"

# ---------------------------------------------------------------------------
# Deny by default — the property every other rule depends on
# ---------------------------------------------------------------------------
test_empty_input_is_denied if {
	not authz.allow with input as {}
}

test_unknown_role_is_denied if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "janitor", "tenant_id": TENANT_A},
		"resource": {"type": "session", "tenant_id": TENANT_A},
	}
}

test_unknown_action_is_denied if {
	not authz.allow with input as {
		"action": "exfiltrate",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "session", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

test_unknown_resource_type_is_denied if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "billing_ledger", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

# ---------------------------------------------------------------------------
# §5.3 check 1 — nurse must NOT reach admin functionality
# ---------------------------------------------------------------------------
test_nurse_cannot_read_tenant_config if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "tenant", "tenant_id": TENANT_A},
	}
}

test_nurse_cannot_manage_devices if {
	not authz.allow with input as {
		"action": "write",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "device", "tenant_id": TENANT_A},
	}
}

test_nurse_cannot_manage_users if {
	not authz.allow with input as {
		"action": "write",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "app_user", "tenant_id": TENANT_A},
	}
}

test_nurse_cannot_export_audit if {
	not authz.allow with input as {
		"action": "export",
		"user": {
			"role": "nurse", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_MED, "mfa_satisfied": true,
		},
		"resource": {"type": "audit_event", "tenant_id": TENANT_A},
	}
}

test_nurse_cannot_approve_a_review if {
	not authz.allow with input as {
		"action": "approve",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}

test_nurse_cannot_edit_a_clinical_fact if {
	not authz.allow with input as {
		"action": "edit_fact",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "clinical_fact", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

# ---------------------------------------------------------------------------
# §5.3 check 2 — a patient must NOT reach another patient's session
# ---------------------------------------------------------------------------
test_patient_can_read_own_session if {
	authz.allow with input as {
		"action": "read",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"patient_id": PATIENT_1, "status": "in_progress",
		},
	}
}

test_patient_cannot_read_another_patients_session if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"patient_id": PATIENT_2, "status": "in_progress",
		},
	}
}

test_patient_cannot_answer_another_patients_session if {
	not authz.allow with input as {
		"action": "answer",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"patient_id": PATIENT_2, "status": "in_progress",
		},
	}
}

test_patient_cannot_read_across_tenants if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "session", "tenant_id": TENANT_B,
			"patient_id": PATIENT_1, "status": "in_progress",
		},
	}
}

test_patient_cannot_reach_a_staff_console if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {"type": "red_flag_alert", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

test_patient_cannot_approve if {
	not authz.allow with input as {
		"action": "approve",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"patient_id": PATIENT_1, "status": "under_review",
		},
	}
}

# ---------------------------------------------------------------------------
# §5.3 check 3 — a physician must NOT reach another department
# ---------------------------------------------------------------------------
test_physician_can_read_own_department if {
	authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}

test_physician_cannot_read_other_department if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "under_review",
		},
	}
}

test_physician_cannot_approve_other_department if {
	not authz.allow with input as {
		"action": "approve",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "under_review",
		},
	}
}

test_physician_cannot_read_other_tenant if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "session", "tenant_id": TENANT_B,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}

test_physician_with_no_department_is_denied if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": null},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}

# ---------------------------------------------------------------------------
# §21 [RED LINE] — an exported session is sealed
# ---------------------------------------------------------------------------
test_physician_cannot_edit_after_export if {
	not authz.allow with input as {
		"action": "edit_fact",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "clinical_fact", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "exported",
		},
	}
}

test_physician_cannot_approve_after_export if {
	not authz.allow with input as {
		"action": "approve",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "exported",
		},
	}
}

test_physician_can_still_read_after_export if {
	# Reading a sealed record must remain possible — sealing prevents writes, not
	# clinical review of what was exported.
	authz.allow with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "exported",
		},
	}
}

test_patient_cannot_answer_after_export if {
	not authz.allow with input as {
		"action": "answer",
		"user": {"role": "patient", "tenant_id": TENANT_A, "patient_id": PATIENT_1},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"patient_id": PATIENT_1, "status": "exported",
		},
	}
}

# ---------------------------------------------------------------------------
# §6 — caregiver respondent scope
# ---------------------------------------------------------------------------
test_caregiver_can_answer_authorized_session if {
	authz.allow with input as {
		"action": "answer",
		"user": {
			"role": "caregiver_respondent", "tenant_id": TENANT_A,
			"authorized_session_ids": [SESSION_1],
		},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"session_id": SESSION_1, "status": "in_progress",
		},
	}
}

test_caregiver_cannot_answer_unauthorized_session if {
	not authz.allow with input as {
		"action": "answer",
		"user": {
			"role": "caregiver_respondent", "tenant_id": TENANT_A,
			"authorized_session_ids": [SESSION_1],
		},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"session_id": SESSION_2, "status": "in_progress",
		},
	}
}

test_caregiver_cannot_grant_consent_through_policy if {
	# [RED LINE §6] Consent-granting is never a caregiver capability. The policy
	# omits it entirely, so even a documented-authority caregiver reaches the
	# consent module's own authority check rather than being waved through here.
	not authz.allow with input as {
		"action": "grant_consent",
		"user": {
			"role": "caregiver_respondent", "tenant_id": TENANT_A,
			"authorized_session_ids": [SESSION_1],
		},
		"resource": {
			"type": "consent", "tenant_id": TENANT_A,
			"session_id": SESSION_1, "status": "in_progress",
		},
	}
}

test_caregiver_with_no_authorized_sessions_is_denied if {
	not authz.allow with input as {
		"action": "answer",
		"user": {
			"role": "caregiver_respondent", "tenant_id": TENANT_A,
			"authorized_session_ids": [],
		},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"session_id": SESSION_1, "status": "in_progress",
		},
	}
}

# ---------------------------------------------------------------------------
# Nurse — permitted within their own department only
# ---------------------------------------------------------------------------
test_nurse_can_acknowledge_own_department_alert if {
	authz.allow with input as {
		"action": "acknowledge",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "red_flag_alert", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "open",
		},
	}
}

test_nurse_cannot_acknowledge_other_department_alert if {
	not authz.allow with input as {
		"action": "acknowledge",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "red_flag_alert", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "open",
		},
	}
}

test_nurse_can_perform_staff_assisted_capture if {
	# §9: the no-phone fallback is a nurse capability by design.
	authz.allow with input as {
		"action": "upload_document",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "document", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "in_progress",
		},
	}
}

# ---------------------------------------------------------------------------
# Clinical Admin (Governance) — content only, never clinical records
# ---------------------------------------------------------------------------
test_clinical_admin_can_read_protocol_content if {
	authz.allow with input as {
		"action": "read",
		"user": {"role": "clinical_admin", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "protocol_version", "tenant_id": TENANT_A},
	}
}

test_clinical_admin_cannot_read_clinical_facts if {
	not authz.allow with input as {
		"action": "read",
		"user": {
			"role": "clinical_admin", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_MED, "mfa_satisfied": true,
		},
		"resource": {"type": "clinical_fact", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

test_clinical_admin_cannot_write_protocol_content if {
	# §5.2: governance changes route through the CI gate, never through the API.
	not authz.allow with input as {
		"action": "write",
		"user": {"role": "clinical_admin", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "protocol_version", "tenant_id": TENANT_A},
	}
}

# ---------------------------------------------------------------------------
# IT Admin — configuration only, and writes need step-up MFA
# ---------------------------------------------------------------------------
test_it_admin_can_read_config_without_step_up if {
	authz.allow with input as {
		"action": "read",
		"user": {"role": "it_admin", "tenant_id": TENANT_A, "mfa_satisfied": false},
		"resource": {"type": "device", "tenant_id": TENANT_A},
	}
}

test_it_admin_write_requires_step_up_mfa if {
	not authz.allow with input as {
		"action": "write",
		"user": {"role": "it_admin", "tenant_id": TENANT_A, "mfa_satisfied": false},
		"resource": {"type": "device", "tenant_id": TENANT_A},
	}
}

test_it_admin_write_allowed_with_step_up_mfa if {
	authz.allow with input as {
		"action": "write",
		"user": {"role": "it_admin", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "device", "tenant_id": TENANT_A},
	}
}

test_it_admin_cannot_read_clinical_data if {
	not authz.allow with input as {
		"action": "read",
		"user": {"role": "it_admin", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "clinical_fact", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

test_it_admin_cannot_touch_other_tenant if {
	not authz.allow with input as {
		"action": "write",
		"user": {"role": "it_admin", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "device", "tenant_id": TENANT_B},
	}
}

# ---------------------------------------------------------------------------
# Security / Privacy Officer — audit and posture, never clinical editing
# ---------------------------------------------------------------------------
test_security_officer_can_read_consent_status if {
	authz.allow with input as {
		"action": "read",
		"user": {"role": "security_officer", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "consent_status", "tenant_id": TENANT_A},
	}
}

test_security_officer_audit_export_requires_step_up if {
	not authz.allow with input as {
		"action": "export",
		"user": {"role": "security_officer", "tenant_id": TENANT_A, "mfa_satisfied": false},
		"resource": {"type": "audit_event", "tenant_id": TENANT_A},
	}
}

test_security_officer_audit_export_allowed_with_step_up if {
	authz.allow with input as {
		"action": "export",
		"user": {"role": "security_officer", "tenant_id": TENANT_A, "mfa_satisfied": true},
		"resource": {"type": "audit_event", "tenant_id": TENANT_A},
	}
}

test_security_officer_cannot_edit_clinical_facts if {
	not authz.allow with input as {
		"action": "edit_fact",
		"user": {
			"role": "security_officer", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_MED, "mfa_satisfied": true,
		},
		"resource": {"type": "clinical_fact", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

test_security_officer_cannot_approve if {
	not authz.allow with input as {
		"action": "approve",
		"user": {
			"role": "security_officer", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_MED, "mfa_satisfied": true,
		},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}

test_security_officer_cannot_read_clinical_facts if {
	not authz.allow with input as {
		"action": "read",
		"user": {
			"role": "security_officer", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_MED, "mfa_satisfied": true,
		},
		"resource": {"type": "clinical_fact", "tenant_id": TENANT_A, "department_id": DEPT_MED},
	}
}

# ---------------------------------------------------------------------------
# AYUSH practitioner — clinical authority plus terminology confirmation (§24)
# ---------------------------------------------------------------------------
test_ayush_practitioner_can_confirm_coding if {
	authz.allow with input as {
		"action": "confirm_coding",
		"user": {
			"role": "ayush_practitioner", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_AYUSH,
		},
		"resource": {
			"type": "namaste_mapping", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "under_review",
		},
	}
}

test_nurse_cannot_confirm_coding if {
	not authz.allow with input as {
		"action": "confirm_coding",
		"user": {"role": "nurse", "tenant_id": TENANT_A, "assigned_department_id": DEPT_AYUSH},
		"resource": {
			"type": "namaste_mapping", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "under_review",
		},
	}
}

test_ayush_practitioner_cannot_confirm_coding_after_export if {
	not authz.allow with input as {
		"action": "confirm_coding",
		"user": {
			"role": "ayush_practitioner", "tenant_id": TENANT_A,
			"assigned_department_id": DEPT_AYUSH,
		},
		"resource": {
			"type": "namaste_mapping", "tenant_id": TENANT_A,
			"department_id": DEPT_AYUSH, "status": "exported",
		},
	}
}

# ---------------------------------------------------------------------------
# Reason codes — used for the 403 body and decision logs
# ---------------------------------------------------------------------------
test_reason_is_tenant_mismatch if {
	authz.reason == "tenant_mismatch" with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "session", "tenant_id": TENANT_B, "department_id": DEPT_MED},
	}
}

test_reason_is_department_mismatch if {
	authz.reason == "department_mismatch" with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {"type": "session", "tenant_id": TENANT_A, "department_id": DEPT_AYUSH},
	}
}

test_reason_is_sealed_after_export if {
	authz.reason == "session_sealed_after_export" with input as {
		"action": "approve",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "physician_review", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "exported",
		},
	}
}

test_reason_is_allow_when_permitted if {
	authz.reason == "allow" with input as {
		"action": "read",
		"user": {"role": "physician", "tenant_id": TENANT_A, "assigned_department_id": DEPT_MED},
		"resource": {
			"type": "session", "tenant_id": TENANT_A,
			"department_id": DEPT_MED, "status": "under_review",
		},
	}
}
