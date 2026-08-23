# Validation notes

## Browser smoke test

- Landing page loaded at `http://localhost:5173/` with the new MediKiosk hero, trust signals, four role cards, network metrics, and AI safety disclaimer.
- Patient role navigation correctly changed the URL to `/intake` and rendered the new accessible registration screen.
- Registration Continue correctly advanced to the language screen.
- Language screen rendered nine supported languages as large selectable cards and displayed the audio guidance affordance.
- Screenshot review showed the landing and kiosk screens have coherent spacing, strong contrast, and large touch targets at the current desktop viewport.

## Build status

- `npm run build` passes for shared, client, and server workspaces after the UI overhaul.
- Fixed existing server workspace issues by removing the invalid server `rootDir`, narrowing timeline provenance to the shared contract, and adding the missing shared package build script.

The Hindi selection correctly updated the top-bar language indicator and opened the consent gate. Consent toggles and the “I understand & continue” action rendered correctly. The conversation screen then opened with a simple body-area selector, large voice control, progress indicator, audio affordance, and disabled Continue state until an answer is selected. Visual inspection showed the low-literacy layout is calm, touch-oriented, and readable.

The physician route rendered the enterprise sidebar, operational hero, KPI strip, searchable queue, urgency signals, completeness bars, and live-looking clinical records. Selecting a queue row opened the summary review center with ABHA status, allergy/comorbidity tags, AI-draft sign-off state, conflict callout, provenance labels, confidence pills, per-section Accept/Edit/Exclude controls, source evidence, a safety guardrail, and an approve-and-push action. No runtime error appeared during the interaction.

The first staff-console attempt exposed a runtime issue caused by accepting an unexpected API alert shape. The console was hardened to retain the demo-safe typed alert model unless the API returns the expected string patient field. After the fix, the staff route rendered successfully with the live safety feed, critical alert card, SLA timer, acknowledge/dispatch/resolve controls, six-station monitor, and operational KPIs.

The governance hub rendered successfully with an operations overview, healthy-system callout, four KPI cards, a responsive throughput chart, access-mix donut, governance controls, attention items, and sidebar navigation for protocols, safety rules, and the immutable audit trail. The visual system remained consistent across all four role experiences.
