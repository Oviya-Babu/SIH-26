# MediKiosk Staff Frontend

The Phase 2 physician workspace: review the API-authorized queue, inspect fact
provenance and red flags, view the review audit trail, and explicitly attest an
approval. It is deliberately not a security boundary; the FastAPI RBAC, OPA and
PostgreSQL RLS layers authorize every request.

## Local development

Start the API and Keycloak stack first, then run:

```bash
npm install
npm run dev
```

The app listens on `http://localhost:3200`, which is already registered as a
Keycloak redirect URI. It uses authorization code + PKCE against Keycloak and
keeps the access token in memory only. Set these public build-time values only
when non-default local endpoints are needed:

```bash
NEXT_PUBLIC_API_ORIGIN=http://localhost:8000
NEXT_PUBLIC_OIDC_ISSUER=http://localhost:8080/realms/medikiosk
NEXT_PUBLIC_OIDC_CLIENT_ID=medikiosk-staff
```

Do not put a client secret or patient data in frontend environment variables.
