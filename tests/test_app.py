import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
