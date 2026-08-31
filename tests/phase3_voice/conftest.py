"""Phase 3 pytest configuration and fixtures."""

from __future__ import annotations

import pytest
from uuid import UUID
from fastapi.testclient import TestClient
from medikiosk.context import AppContext
from medikiosk.main import create_app


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    import asyncio
    
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def app():
    """Create test FastAPI app."""
    return create_app(connect_db=True)


@pytest.fixture
def client(app) -> TestClient:
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def app_ctx(app) -> AppContext:
    """Get application context."""
    return app.state.ctx


@pytest.fixture
def ai_gateway_client(app_ctx):
    """Get AI Gateway client."""
    return app_ctx.ai


@pytest.fixture
def test_device_token():
    """Mock device token for kiosk authentication.
    
    In real tests, this comes from the seed_demo.py output.
    """
    return "mock-device-token-phase3"


@pytest.fixture
def test_session_token():
    """Mock session token."""
    return "mock-session-token-phase3"


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
