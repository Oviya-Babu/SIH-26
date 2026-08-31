"""Phase 3 pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from medikiosk.ai.gateway_client import AIGatewayClient
from medikiosk.config import Settings
from medikiosk.context import AppContext
from medikiosk.main import create_app
from medikiosk.security.tokens import TokenService
from medikiosk_ai.main import app as ai_app


@pytest.fixture
def ai_client():
    """Test client for AI Gateway with lifespan started."""
    with TestClient(ai_app) as client:
        yield client


@pytest.fixture
def app():
    """Create test FastAPI app."""
    return create_app(connect_db=False)


@pytest.fixture
def client(app) -> TestClient:
    """Create test client with lifespan context."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def app_ctx(client) -> AppContext:
    """Get application context after lifespan entered."""
    return client.app.state.ctx


@pytest_asyncio.fixture
async def ai_gateway_client(ai_client):
    """Get AI Gateway client connected to in-process AI Gateway."""
    settings = Settings(ai_gateway_url="http://testserver")
    transport = httpx.ASGITransport(app=ai_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield AIGatewayClient(settings, http)


@pytest.fixture
def token_service() -> TokenService:
    return TokenService("local-dev-only-not-a-secret")


@pytest.fixture
def test_tenant_id() -> UUID:
    """Test tenant ID (matching seed_demo)."""
    return UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def test_patient_id() -> UUID:
    """Test patient ID."""
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_session_id() -> UUID:
    """Test session ID."""
    return UUID("10000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_department_id() -> UUID:
    """Test department ID (General Medicine)."""
    return UUID("20000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_device_id() -> UUID:
    """Test device ID."""
    return UUID("30000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_device_token(
    token_service: TokenService,
    test_tenant_id: UUID,
    test_department_id: UUID,
    test_device_id: UUID,
) -> str:
    """HMAC-signed kiosk device token for testing."""
    token, _ = token_service.mint(
        "kiosk",
        tenant_id=test_tenant_id,
        ttl_seconds=3600,
        device_id=test_device_id,
        department_id=test_department_id,
    )
    return token


@pytest.fixture
def test_session_token(
    token_service: TokenService,
    test_tenant_id: UUID,
    test_session_id: UUID,
    test_patient_id: UUID,
    test_department_id: UUID,
) -> str:
    """HMAC-signed session token for testing."""
    token, _ = token_service.mint(
        "session",
        tenant_id=test_tenant_id,
        ttl_seconds=3600,
        session_id=test_session_id,
        patient_id=test_patient_id,
        department_id=test_department_id,
        subject_role="patient",
        actor_id=test_patient_id,
    )
    return token
