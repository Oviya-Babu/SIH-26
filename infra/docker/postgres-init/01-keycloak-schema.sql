-- =============================================================================
-- Create Keycloak's own schema, before Keycloak starts.
--
-- Keycloak stores ~90 tables. Those must NOT land in `public`, which is the
-- clinical schema: MediKiosk's RLS audit enumerates every table in `public` and
-- asserts row security is enabled and forced on each patient-data table (§30).
-- Keycloak's tables would show up there as unprotected noise, and a reviewer
-- would have to squint past them to see whether the real invariant holds.
--
-- Keycloak does not create its own schema when KC_DB_SCHEMA is set, so it is
-- created here, at cluster initialisation.
--
-- This runs ONCE, when the postgres data volume is first initialised.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS keycloak;

COMMENT ON SCHEMA keycloak IS
    'Keycloak identity store. Deliberately separate from public, which holds the '
    'clinical schema under Row Level Security (CLAUDE.md §30).';
