"""Request dependencies — the auth chain, in order (CLAUDE.md §5.1).

    Request → TLS → OIDC → RBAC → OPA → app rule → RLS

Each link is a separate object with a separate failure mode, and none of them
trusts the one above it. The only way a handler obtains a :class:`Principal`
(and therefore a database connection) is by declaring which
:class:`Capability` it needs, so "deny by default" is the structural default
rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable
from uuid import UUID

from fastapi import Depends, Request

from medikiosk.context import AppContext
from medikiosk.db import Principal
from medikiosk.errors import AuthenticationRequired, Forbidden, NotFound
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security import rbac
from medikiosk.security.oidc import AuthenticationError, StaffClaims
from medikiosk.security.opa import ResourceContext, build_input
from medikiosk.security.rbac import Capability
from medikiosk.security.tokens import TokenClaims, TokenError

log = get_logger(__name__)


def get_ctx(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise RuntimeError("application context is not initialised")
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationRequired(
            "missing bearer token", reason_code="authentication_required"
        )
    return token.strip()


# ---------------------------------------------------------------------------
# Staff tier — OIDC
# ---------------------------------------------------------------------------
async def staff_claims(request: Request, ctx: Ctx) -> StaffClaims:
    token = _bearer(request)
    try:
        return await ctx.oidc.verify(token)
    except AuthenticationError as exc:
        log.info(
            "staff_auth_failed",
            auth_stage="oidc",
            reason_code=exc.reason_code,
            http_route=request.url.path,
        )
        raise AuthenticationRequired(exc.message, reason_code=exc.reason_code) from exc


async def staff_principal(
    request: Request,
    ctx: Ctx,
    claims: Annotated[StaffClaims, Depends(staff_claims)],
) -> Principal:
    """Project the OIDC identity onto the local user row.

    Keycloak is the identity authority, but department assignment and account
    status are tenant configuration, so the local projection is what OPA sees.
    A token for a disabled or unknown user is refused here, not later.
    """
    bootstrap = Principal(tenant_id=claims.tenant_id, role=claims.role, subject=claims.subject)
    async with ctx.db.readonly(bootstrap) as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.role, u.assigned_department_id, u.status, u.mfa_enrolled
              FROM app_user u
             WHERE u.subject = $1
            """,
            claims.subject,
        )

    if row is None:
        # RLS also hides users from other tenants, so "not found" here covers
        # both "no such user" and "not in your tenant" without disclosing which.
        log.info("staff_auth_failed", auth_stage="local_projection",
                 reason_code="user_not_provisioned", actor_role=claims.role)
        raise AuthenticationRequired(
            "user is not provisioned in this tenant", reason_code="user_not_provisioned"
        )
    if row["status"] != "active":
        raise AuthenticationRequired("account is disabled", reason_code="account_disabled")
    if row["role"] != claims.role:
        # The local role and the token role disagree: refuse rather than pick one.
        raise AuthenticationRequired(
            "role assignment mismatch", reason_code="role_mismatch"
        )

    return Principal(
        tenant_id=claims.tenant_id,
        role=claims.role,
        actor_id=row["id"],
        department_id=row["assigned_department_id"],
        mfa_satisfied=claims.mfa_satisfied,
        subject=claims.subject,
    )


StaffPrincipal = Annotated[Principal, Depends(staff_principal)]


# ---------------------------------------------------------------------------
# Kiosk tier — device and session tokens
# ---------------------------------------------------------------------------
async def kiosk_token(request: Request, ctx: Ctx) -> TokenClaims:
    token = _bearer(request)
    try:
        return ctx.tokens.verify(token, expect="kiosk")
    except TokenError as exc:
        raise AuthenticationRequired(exc.message, reason_code=exc.reason_code) from exc


async def kiosk_principal(
    ctx: Ctx,
    claims: Annotated[TokenClaims, Depends(kiosk_token)],
) -> Principal:
    """A device-scoped principal, before a patient is identified.

    §8: tenant and department are fixed BY THE DEVICE and are never taken from
    the request body.
    """
    return Principal(
        tenant_id=claims.tenant_id,
        role="kiosk_device",
        department_id=claims.department_id,
        actor_id=claims.device_id,
    )


KioskPrincipal = Annotated[Principal, Depends(kiosk_principal)]


async def session_token(request: Request, ctx: Ctx) -> TokenClaims:
    token = _bearer(request)
    try:
        return ctx.tokens.verify(token, expect="session")
    except TokenError as exc:
        raise AuthenticationRequired(exc.message, reason_code=exc.reason_code) from exc


async def session_principal(
    ctx: Ctx,
    claims: Annotated[TokenClaims, Depends(session_token)],
) -> Principal:
    """The patient/caregiver principal for exactly one session.

    The token carries the session and patient; nothing in the request body can
    widen it. Combined with the RLS patient-self policies, a patient token
    physically cannot read another patient's row (§30, §64.8).
    """
    role = "caregiver_respondent" if claims.subject_role == "caregiver_respondent" else "patient"
    return Principal(
        tenant_id=claims.tenant_id,
        role=role,
        actor_id=claims.patient_id,
        patient_id=claims.patient_id,
        session_id=claims.session_id,
        department_id=claims.department_id,
        authorized_session_ids=(claims.session_id,) if claims.session_id else (),
    )


SessionPrincipal = Annotated[Principal, Depends(session_principal)]


# ---------------------------------------------------------------------------
# Authorization — RBAC then OPA
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Authorizer:
    """Callable guard bound to one capability and one OPA action."""

    ctx: AppContext
    principal: Principal
    capability: Capability
    action: str

    async def check(self, resource: ResourceContext) -> None:
        # 1. RBAC — does this role have this endpoint at all?
        if not rbac.has_capability(self.principal.role, self.capability):
            log.info(
                "authz_denied",
                auth_stage="rbac",
                actor_role=self.principal.role,
                action=self.action,
                entity_type=resource.type,
                reason_code="capability_not_granted",
            )
            raise Forbidden(
                "role does not hold this capability", reason_code="capability_not_granted"
            )

        # 2. Step-up MFA for the highest-privilege capabilities.
        if self.capability in rbac.STEP_UP_CAPABILITIES and not self.principal.mfa_satisfied:
            log.info(
                "authz_denied",
                auth_stage="step_up",
                actor_role=self.principal.role,
                action=self.action,
                reason_code="step_up_required",
            )
            raise Forbidden("step-up authentication required", reason_code="step_up_required")

        # 3. OPA — this identity, on THIS resource, in context.
        decision = await self.ctx.opa.evaluate(
            build_input(
                action=self.action,
                role=self.principal.role,
                tenant_id=self.principal.tenant_id,
                resource=resource,
                actor_id=self.principal.actor_id,
                assigned_department_id=self.principal.department_id,
                patient_id=self.principal.patient_id,
                mfa_satisfied=self.principal.mfa_satisfied,
                authorized_session_ids=self.principal.authorized_session_ids,
            )
        )
        if not decision.allow:
            log.info(
                "authz_denied",
                auth_stage="opa",
                actor_role=self.principal.role,
                action=self.action,
                entity_type=resource.type,
                reason_code=decision.reason_code,
                opa_decision="deny",
            )
            raise Forbidden("not permitted on this resource", reason_code=decision.reason_code)

        # 4. The app-layer business rule runs in the handler, and 5. RLS runs in
        #    the database. Neither is skipped because this returned.


def require(
    capability: Capability,
    action: str,
    *,
    tier: str = "staff",
) -> Callable[..., Awaitable[Authorizer]]:
    """Build a dependency yielding an :class:`Authorizer`.

    ``tier`` selects which authentication link applies. A staff endpoint cannot
    accidentally accept a kiosk token, because the dependency itself differs.

    Each tier has its own closure with a MODULE-LEVEL annotation alias. That is
    deliberate: ``from __future__ import annotations`` makes annotations strings,
    and FastAPI resolves them against module globals — a closure variable would
    not resolve, so the three branches are written out rather than parameterised.
    """
    if tier == "staff":

        async def dependency(ctx: Ctx, principal: StaffPrincipal) -> Authorizer:
            return Authorizer(
                ctx=ctx, principal=principal, capability=capability, action=action
            )

    elif tier == "kiosk":

        async def dependency(ctx: Ctx, principal: KioskPrincipal) -> Authorizer:  # type: ignore[misc]
            return Authorizer(
                ctx=ctx, principal=principal, capability=capability, action=action
            )

    elif tier == "session":

        async def dependency(ctx: Ctx, principal: SessionPrincipal) -> Authorizer:  # type: ignore[misc]
            return Authorizer(
                ctx=ctx, principal=principal, capability=capability, action=action
            )

    else:
        raise ValueError(f"unknown authentication tier: {tier}")

    return dependency


# ---------------------------------------------------------------------------
# Small shared loaders
# ---------------------------------------------------------------------------
async def load_session_row(conn, session_id: UUID):
    row = await conn.fetchrow(
        """
        SELECT s.*, pr.status AS review_status, d.code AS department_code
          FROM session s
          JOIN department d ON d.id = s.department_id
          LEFT JOIN physician_review pr ON pr.session_id = s.id
         WHERE s.id = $1
        """,
        session_id,
    )
    if row is None:
        raise NotFound("session not found", reason_code="not_found")
    return row


def session_resource(row) -> ResourceContext:
    """Build the OPA resource document for a session row.

    ``status`` carries the *review* status when one exists, because that is what
    the policy's post-export seal checks (§21).
    """
    return ResourceContext(
        type="session",
        id=row["id"],
        tenant_id=row["tenant_id"],
        department_id=row["department_id"],
        patient_id=row["patient_id"],
        status=row["review_status"] or row["status"],
        extra={"session_id": str(row["id"])},
    )
