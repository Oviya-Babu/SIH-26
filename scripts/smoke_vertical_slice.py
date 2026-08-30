#!/usr/bin/env python3
"""Live vertical-slice smoke test (CLAUDE.md §57 Phase 2 DoD, §64).

Drives the EXACT slice §57 Phase 2 names, against a running deployment over HTTP:

    kiosk → identity → consent → department → protocol → question → answer
          → clinical fact → next question → red flag → completion

Then asserts the properties that make it a clinical system rather than a form:

* the protocol drives question order, and a red-flag answer collapses the
  interview to AMPLE (§14);
* every fact carries provenance and a respondent (§13);
* the escalation is visible to staff and calm to the patient (§14);
* the transient purge ran at submission (§38).

    python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000 \\
        --device-credential <credential from seed_demo>
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36m ›  \033[0m"


class SmokeFailure(AssertionError):
    pass


class Smoke:
    def __init__(self, base_url: str, device_credential: str, language: str) -> None:
        self.base = base_url.rstrip("/")
        self.credential = device_credential
        self.language = language
        self.client = httpx.Client(timeout=30.0)
        self.checks: list[tuple[bool, str]] = []

    def check(self, condition: bool, description: str) -> None:
        self.checks.append((bool(condition), description))
        marker = PASS if condition else FAIL
        print(f"  {marker}  {description}")
        if not condition:
            raise SmokeFailure(description)

    def note(self, message: str) -> None:
        print(f"{INFO}{message}")

    def post(self, path: str, token: str | None = None, **kwargs) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return self.client.post(f"{self.base}{path}", headers=headers, **kwargs)

    def get(self, path: str, token: str | None = None, **kwargs) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return self.client.get(f"{self.base}{path}", headers=headers, **kwargs)

    # -- the slice ---------------------------------------------------------
    def run(self) -> int:
        print("\n\033[1mMediKiosk vertical slice — live against the deployed API\033[0m")

        print("\n[0] readiness")
        ready = self.get("/readyz").json()
        self.check(ready["ready"], "service reports ready")
        protocols = {p["family"] for p in ready["checks"]["clinical_content"]["protocols"]}
        self.check(
            protocols == {"general_medicine", "ayush_ayurveda"},
            f"both protocol families loaded: {sorted(protocols)}",
        )

        print("\n[1] supported languages (§18, five-language prototype)")
        languages = self.get("/v1/meta/languages").json()
        codes = [entry["code"] for entry in languages["languages"]]
        self.check(codes == ["en", "hi", "ta", "te", "ml"], f"exactly five languages: {codes}")
        endonyms = {entry["code"]: entry["endonym"] for entry in languages["languages"]}
        self.check(
            endonyms["ta"] == "தமிழ்" and endonyms["ml"] == "മലയാളം",
            "endonyms render in their own script",
        )

        print("\n[2] device authentication (§8, §33)")
        bad = self.post(
            "/v1/kiosk/device/token",
            json={"device_credential": "x" * 48},
        )
        self.check(bad.status_code == 404, f"unprovisioned credential refused ({bad.status_code})")

        response = self.post(
            "/v1/kiosk/device/token", json={"device_credential": self.credential}
        )
        self.check(response.status_code == 200, f"provisioned device accepted ({response.status_code})")
        device = response.json()
        kiosk_token = device["kiosk_token"]
        department = device["department"]
        self.check(department is not None, "device fixes the department (§8)")
        self.note(
            f"tenant={device['tenant_name']!r} department={department['display_name']!r} "
            f"protocol_family={department['protocol_family']!r}"
        )

        print("\n[3] localized kiosk content (no hardcoded copy in the frontend)")
        bundle = self.get(f"/v1/kiosk/i18n/{self.language}", kiosk_token).json()
        self.check(bundle["language"] == self.language, f"bundle served for {self.language!r}")
        self.check(
            bool(bundle["bundles"]["kiosk"]["welcome"]["start_spoken"]),
            "spoken TTS script present for the welcome screen",
        )
        self.check(
            bool(bundle["bundles"]["consent"]["purposes"]["staff_access"]["spoken"]),
            "consent notice has an audio script (§7.2)",
        )

        print("\n[4] identity — local registration path (§7.1)")
        aadhaar_attempt = self.post(
            "/v1/kiosk/identify",
            kiosk_token,
            json={
                "mode": "local",
                "full_name": "Aadhaar Attempt",
                "hospital_local_id": "234567890123",
                "preferred_language": self.language,
            },
        )
        self.check(
            aadhaar_attempt.status_code == 422
            and aadhaar_attempt.json()["error"]["reason_code"] == "aadhaar_not_accepted",
            "Aadhaar-shaped identifier refused (§7.1 [RED LINE])",
        )

        identify = self.post(
            "/v1/kiosk/identify",
            kiosk_token,
            json={
                "mode": "local",
                "full_name": "Synthetic Smoke Patient",
                "year_of_birth": 1972,
                "gender": "male",
                "phone_last4": "4321",
                "preferred_language": self.language,
            },
        )
        self.check(identify.status_code == 200, f"local registration succeeded ({identify.status_code})")
        patient = identify.json()
        patient_id = patient["patient_id"]
        self.check(patient["consent_required"], "consent is required before proceeding")
        self.note(f"patient local id = {patient['hospital_local_id']}")

        print("\n[5] session start WITHOUT consent must be refused (§7.2)")
        premature = self.post(
            "/v1/sessions",
            kiosk_token,
            json={
                "patient_id": patient_id,
                "department_id": department["id"],
                "language": self.language,
            },
        )
        self.check(
            premature.status_code == 403
            and premature.json()["error"]["reason_code"] == "consent_required",
            "interview blocked until internal consent exists",
        )

        print("\n[6] internal consent (§7.2)")
        consent = self.post(
            "/v1/consents",
            kiosk_token,
            json={
                "patient_id": patient_id,
                "notice_language": self.language,
                "audio_explained": True,
                "grantor_type": "patient",
                "decisions": [
                    {"purpose": "staff_access", "granted": True},
                    {"purpose": "ai_processing", "granted": True},
                    {"purpose": "document_processing", "granted": True},
                    {"purpose": "voice_capture", "granted": False},
                    {"purpose": "abdm_sharing_intent", "granted": False},
                ],
            },
        )
        self.check(consent.status_code == 200, f"consent recorded ({consent.status_code})")
        consent_body = consent.json()
        self.check(consent_body["may_proceed"], "staff access granted → may proceed")
        self.check(
            "voice_capture" in consent_body["refused"],
            "a refused purpose is recorded as refused, not silently granted",
        )
        self.note(f"notice_version = {consent_body['notice_version']}")

        print("\n[7] session start (§10 protocol resolution)")
        session = self.post(
            "/v1/sessions",
            kiosk_token,
            json={
                "patient_id": patient_id,
                "department_id": department["id"],
                "language": self.language,
            },
        )
        self.check(session.status_code == 200, f"session created ({session.status_code})")
        session_body = session.json()
        session_id = session_body["session_id"]
        session_token = session_body["session_token"]
        self.check(
            session_body["protocol_family"] == department["protocol_family"],
            "department drove protocol family selection (§10)",
        )
        self.note(
            f"protocol {session_body['protocol_family']}:{session_body['protocol_version']} "
            f"checksum={session_body['protocol_checksum']} "
            f"~{session_body['estimated_questions']} required questions"
        )

        print("\n[8] voice answer must be refused (voice consent was declined)")
        first = self.get(f"/v1/sessions/{session_id}/next-question", session_token).json()
        voice_attempt = self.post(
            f"/v1/sessions/{session_id}/answers",
            session_token,
            json={
                "field_id": first["question"]["field_id"],
                "value": first["question"]["options"][0]["value"],
                "input_method": "voice",
                "confidence": 0.95,
            },
        )
        self.check(
            voice_attempt.status_code == 403
            and voice_attempt.json()["error"]["reason_code"] == "consent_required",
            "voice input blocked because voice_capture consent was declined",
        )

        print("\n[9] the interview — deterministic order, localized rendering")
        question = first["question"]
        self.check(
            question["field_id"] == "gm.cc.primary_complaint",
            f"first question is the chief complaint: {question['field_id']}",
        )
        self.check(
            bool(question["voice_prompt"]) and bool(question["touch_label"]),
            "question rendered with both a spoken prompt and a short touch label",
        )
        self.check(
            all(option["label"] for option in question["options"]),
            f"all {len(question['options'])} options carry a localized label",
        )
        self.check(
            all(option["icon"] for option in question["options"]),
            "all options carry a semantic icon (low-literacy support, §1)",
        )
        self.note(f"prompt: {question['voice_prompt'][:100]}")

        # Scripted ACS presentation: this MUST trip the red-flag engine.
        scripted: dict[str, Any] = {
            "gm.cc.primary_complaint": "chest_pain",
            "gm.cc.duration_of_concern": {"value": 2, "unit": "hours"},
            "gm.hpi.site": ["chest_center"],
            "gm.hpi.character": ["crushing"],
            "gm.hpi.radiation": ["left_arm"],
            "gm.hpi.associated_symptoms": ["cold_sweating", "breathlessness"],
            "gm.hpi.time_course": "sudden_and_severe",
            "gm.hpi.exacerbating": ["physical_exertion"],
            "gm.hpi.relieving": ["nothing_helps"],
            "gm.hpi.severity": 9,
        }

        asked: list[str] = []
        escalated = False
        guard = 0
        current = question

        while current is not None:
            guard += 1
            if guard > 80:
                raise SmokeFailure("interview did not terminate")

            field_id = current["field_id"]
            asked.append(field_id)
            value = scripted.get(field_id, _default_answer(current))

            answer = self.post(
                f"/v1/sessions/{session_id}/answers",
                session_token,
                json={
                    "field_id": field_id,
                    "value": value,
                    "input_method": "touch",
                    "confidence": 1.0,
                    "confirmed": True,
                },
            )
            if answer.status_code != 200:
                raise SmokeFailure(
                    f"answer to {field_id} rejected: {answer.status_code} {answer.text[:300]}"
                )
            body = answer.json()

            if body["escalated"] and not escalated:
                escalated = True
                print()
                self.check(True, f"RED FLAG fired on {field_id} → fast path engaged (§14)")
                self.check(
                    body["session_status"] == "escalated_to_staff",
                    "session status is escalated_to_staff, not completed (§14.5)",
                )
                self.check(
                    body["escalation"]["message_key"] == "escalation.body",
                    "kiosk receives an i18n KEY, never clinical rationale (§14)",
                )
                self.note("remaining questions after escalation:")

            current = body.get("next_question")

        print()
        self.check(escalated, "the scripted ACS presentation escalated")
        ample_asked = [f for f in asked if f.startswith("gm.ample.")]
        self.check(
            bool(ample_asked),
            f"AMPLE fast-path questions were asked: {ample_asked}",
        )
        self.check(
            len(ample_asked) <= 5,
            f"fast path completed in a handful of questions ({len(ample_asked)})",
        )
        self.note(f"total questions asked: {len(asked)}")

        print("\n[10] provenance and confirmation view (§13)")
        confirmation = self.get(
            f"/v1/sessions/{session_id}/confirmation", session_token
        ).json()
        facts = confirmation["facts"]
        self.check(bool(facts), f"clinical facts were written ({len(facts)})")
        self.check(
            all(fact["source_type"] == "patient_answer" for fact in facts),
            "every fact is attributed to the patient as respondent (§13)",
        )
        self.check(
            all(fact["label"] for fact in facts),
            "every fact renders a localized label for the patient to check",
        )
        self.check(
            confirmation["completeness"] == 1.0,
            f"completeness reached 1.0 against the reduced AMPLE set "
            f"({confirmation['completeness']})",
        )

        print("\n[11] escalation-skipped questions are attributed correctly (§14.4)")
        answers = confirmation["answers"]
        not_asked = [
            a["field_id"]
            for a in answers
            if a["skip_reason"] == "not_asked_due_to_emergency_escalation"
        ]
        self.check(
            bool(not_asked),
            f"{len(not_asked)} questions marked not_asked_due_to_emergency_escalation",
        )
        self.check(
            all(a["skip_reason"] != "not_answered" for a in answers),
            "no question was left as an ambiguous 'not_answered'",
        )

        print("\n[12] QR-to-phone upload token (§9, §34)")
        upload = self.post(f"/v1/sessions/{session_id}/upload-token", session_token)
        self.check(upload.status_code == 200, f"upload token issued ({upload.status_code})")
        upload_body = upload.json()
        self.check(
            len(upload_body["qr_png_base64"]) > 500,
            "QR code rendered server-side (token never sits in frontend state)",
        )
        self.check(
            upload_body["fallback_available"],
            "staff-assisted capture is offered as a mandatory fallback (§9)",
        )
        self.check(
            10 <= upload_body["ttl_minutes"] <= 60,
            f"token TTL is short-lived ({upload_body['ttl_minutes']} min)",
        )

        print("\n[13] cross-session access is refused (§64.8)")
        other_patient = self.post(
            "/v1/kiosk/identify",
            kiosk_token,
            json={
                "mode": "local",
                "full_name": "Other Synthetic Patient",
                "year_of_birth": 1990,
                "preferred_language": "en",
            },
        ).json()
        self.post(
            "/v1/consents",
            kiosk_token,
            json={
                "patient_id": other_patient["patient_id"],
                "notice_language": "en",
                "audio_explained": True,
                "grantor_type": "patient",
                "decisions": [{"purpose": "staff_access", "granted": True}],
            },
        )
        other_session = self.post(
            "/v1/sessions",
            kiosk_token,
            json={
                "patient_id": other_patient["patient_id"],
                "department_id": department["id"],
                "language": "en",
            },
        ).json()

        cross = self.get(
            f"/v1/sessions/{other_session['session_id']}/next-question", session_token
        )
        self.check(
            cross.status_code == 403,
            f"patient token refused on another patient's session ({cross.status_code})",
        )

        print("\n[14] submission and the synchronous transient purge (§38)")
        submit = self.post(f"/v1/sessions/{session_id}/submit", session_token)
        self.check(submit.status_code == 200, f"session submitted ({submit.status_code})")
        submit_body = submit.json()
        self.check(
            submit_body["status"] == "escalated_to_staff",
            "an escalated session ends as escalated_to_staff, never completed (§14.5)",
        )
        self.check(
            submit_body["transient_purged"],
            "transient session state was purged synchronously at submission",
        )
        self.check(
            submit_body["summary_pending"],
            "the patient is not made to wait for summary generation (§54)",
        )

        print("\n[15] post-submission writes are refused")
        late = self.post(
            f"/v1/sessions/{session_id}/answers",
            session_token,
            json={"field_id": "gm.hpi.severity", "value": 1, "input_method": "touch"},
        )
        self.check(
            late.status_code == 409,
            f"answering a submitted session is refused ({late.status_code})",
        )

        passed = sum(1 for ok, _ in self.checks if ok)
        print(f"\n\033[1m{passed}/{len(self.checks)} checks passed\033[0m")
        print(f"session id: {session_id}")
        return 0 if passed == len(self.checks) else 1


def _default_answer(question: dict[str, Any]) -> Any:
    value_type = question["value_type"]
    options = question["options"]
    if value_type == "boolean":
        return True
    if value_type == "single_select":
        return options[0]["value"]
    if value_type in ("multi_select", "body_region"):
        # Prefer an exclusive "none of these" so a default answer never invents a
        # positive clinical finding.
        exclusive = [o["value"] for o in options if o.get("exclusive")]
        return [exclusive[0]] if exclusive else [options[0]["value"]]
    if value_type == "scale":
        return int(question["validation"]["min"] or 0)
    if value_type == "number":
        units = question["validation"]["units"]
        minimum = question["validation"]["min"] or 1
        return {"value": minimum, "unit": units[0]} if units else minimum
    if value_type == "duration":
        units = question["validation"]["units"] or ["days"]
        return {"value": 3, "unit": units[0]}
    if value_type == "frequency":
        units = question["validation"]["units"] or ["per_day"]
        return units[0]
    if value_type == "date":
        return "2026-01-10"
    return "scripted smoke answer"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--device-credential", required=True)
    parser.add_argument("--language", default="ta", help="interview language code")
    args = parser.parse_args()

    smoke = Smoke(args.base_url, args.device_credential, args.language)
    try:
        return smoke.run()
    except SmokeFailure as exc:
        print(f"\n\033[31mSMOKE FAILED:\033[0m {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"\n\033[31mTRANSPORT ERROR:\033[0m {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
