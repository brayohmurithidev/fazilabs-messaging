import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db_session
from app.main import create_app


def test_application_creation(test_settings) -> None:
    application = create_app(test_settings)
    assert isinstance(application, FastAPI)
    assert application.title == "Test Messaging Platform"


@pytest.mark.asyncio
async def test_root_endpoint(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {"name": "Test Messaging Platform", "environment": "test"}


@pytest.mark.asyncio
async def test_health_endpoint(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_checks_database(app) -> None:
    class HealthySession:
        async def execute(self, statement):
            return None

    async def healthy_session():
        yield HealthySession()

    app.dependency_overrides[get_db_session] = healthy_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_ready_endpoint_sanitizes_database_failure(app) -> None:
    sensitive_error = (
        "postgresql://internal-user:internal-password@private-db.example/production "
        "SELECT 1 asyncpg connection refused"
    )

    class UnavailableSession:
        async def execute(self, statement):
            raise RuntimeError(sensitive_error)

    async def unavailable_session():
        yield UnavailableSession()

    app.dependency_overrides[get_db_session] = unavailable_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "database_unavailable",
            "message": "Database is unavailable.",
        }
    }
    serialized = response.text.lower()
    for forbidden in (
        "internal-user",
        "internal-password",
        "private-db.example",
        "select 1",
        "asyncpg",
        "runtimeerror",
        "traceback",
    ):
        assert forbidden not in serialized
