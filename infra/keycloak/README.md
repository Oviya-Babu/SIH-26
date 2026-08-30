# Keycloak realm — MediKiosk

`realm-medikiosk.json` is imported at container start (`--import-realm`). Keycloak
rejects unknown keys, so the file carries no comments; the rationale lives here.

## Where Keycloak sits in the chain (§5.1)

```
Request → TLS → OIDC (Keycloak: identity + tenant claim)
  → RBAC → OPA → app rule → PostgreSQL RLS
```

Keycloak is the **authentication** authority and the source of the `tenant_id`
claim. It is **not** the authorization authority. A token that says
`realm_access.roles: [physician]` is a claim about identity, not permission —
OPA re-decides on the resource, and RLS re-checks in the database. No layer
trusts the layer above it.

## The seven roles (§5.2)

| Role | Can | Cannot |
|---|---|---|
| `nurse` | Red-flag queue, own department; staff-assisted capture | Physician actions, other departments, admin |
| `physician` | Edit/reject/approve assigned sessions | Post-export writes, other departments/tenants |
| `ayush_practitioner` | Physician authority + NAMASTE/ICD-11 TM2 confirmation | Same limits as physician |
| `clinical_admin` | Protocol and red-flag content, safety metrics | Any clinical record; any write (CI gate instead) |
| `it_admin` | Own-tenant device/user/integration config | Any clinical data; other tenants |
| `security_officer` | Audit export (step-up MFA), consent/retention status | Editing clinical records |
| `caregiver_respondent` | — reserved, see below | — |

`caregiver_respondent` exists as a realm role but is **never assigned**.
Caregivers are not Keycloak identities: a caregiver at a kiosk holds an ephemeral,
session-scoped token (§4, §6). The role name is reserved so it cannot later be
reused for something with different semantics.

## The tenant claim

`tenant_id` is a user attribute mapped into the access token by the
`medikiosk-tenant` client scope. The API reads tenancy from this claim and never
from a request parameter, so a client cannot assert its own tenant.

The uuids in this file are the **fixed** demo tenant ids created by
`scripts/seed_demo.py`:

- `11111111-1111-1111-1111-111111111111` — SIH Demonstration District Hospital
- `22222222-2222-2222-2222-222222222222` — Isolation Control Hospital

The second tenant and its `physician.other` user exist for one purpose: so
cross-tenant isolation can be **proven** with a real identity attempting real
access, rather than asserted (§64.8).

If you re-seed with different tenant ids, update this file to match — otherwise
tokens will carry a tenant that does not exist and RLS will correctly show
nothing, which looks like a bug and is not one.

## MFA (§27)

§27 requires MFA for physician, AYUSH practitioner, clinical admin, IT admin and
security officer. Two mechanisms:

1. The `medikiosk-browser-mfa` authentication flow requires OTP after
   username/password. Bind it as the realm browser flow to enforce it for
   interactive login.
2. The API independently refuses a token for an MFA-required role whose `acr`/`amr`
   claims do not evidence MFA (`security/oidc.py`). This is the layer that actually
   holds, because it does not depend on the realm being configured correctly.

## `directAccessGrantsEnabled` — must be disabled in production

The `medikiosk-staff` client has the resource-owner password grant enabled so
automated tests can obtain a real token without driving a browser.

**That grant bypasses MFA.** It must be turned off before any real-patient
deployment, or the §27 requirement is satisfiable by a script. It is left on here
because this is a synthetic-data development realm (§28) and the alternative —
mocking OIDC in tests — would mean the auth chain is never actually exercised.

## Credentials

Every password in this file is a development placeholder and is marked
`temporary: true`, so Keycloak forces a change at first interactive login.

[RED LINE §32] Real secrets never live in a committed file. Production realm
configuration comes from Vault or a KMS-backed store, applied at deploy time.

## Regenerating

```bash
scripts/compose.sh up -d keycloak          # imports on first start
scripts/compose.sh down -v && scripts/compose.sh up -d keycloak   # re-import
```

Import is idempotent per realm: an existing realm is not overwritten. Drop the
volume to force a clean re-import.
