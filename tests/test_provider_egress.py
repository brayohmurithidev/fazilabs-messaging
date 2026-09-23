"""Live provider egress guard.

No test here reaches the network: provider dispatch is either replaced by a
tripwire that fails the test, or served by an in-process httpx MockTransport.
"""

import argparse
import json
import pathlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app import cli
from app.api.dependencies import get_authenticated_application
from app.api.routes.messages import get_messaging_service
from app.core.config import Environment, Settings
from app.db.session import get_db_session
from app.main import create_app
from app.services import provider_egress
from app.services.advanta_client import AdvantaClient
from app.services.provider_egress import (
    LiveProviderEgressBlockedError,
    build_provider_clients,
    require_live_provider_egress,
)
from app.services.whatsapp_client import WhatsAppCloudAPIClient

PROVIDER_CREDENTIALS = {
    "whatsapp_verify_token": "fake-verify-token",
    "whatsapp_app_secret": "fake-app-secret",
    "whatsapp_access_token": "fake-access-token",
    "whatsapp_phone_number_id": "000000000",
    "whatsapp_api_version": "v99.0",
    "advanta_base_url": "https://advanta.invalid",
    "advanta_api_key": "fake-advanta-key",
    "advanta_partner_id": "fake-partner",
}
PRODUCTION_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/production"


def settings_for(environment: Environment, **overrides) -> Settings:
    values = PROVIDER_CREDENTIALS | overrides
    if environment in {Environment.STAGING, Environment.PRODUCTION}:
        values.setdefault("database_url", PRODUCTION_DATABASE_URL)
    return Settings(_env_file=None, environment=environment, **values)


@pytest.fixture
def provider_tripwire(monkeypatch):
    """Fail the test if any live provider client reaches its network layer."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("live provider network dispatch must not happen")

    monkeypatch.setattr(WhatsAppCloudAPIClient, "_send", forbidden)
    monkeypatch.setattr(AdvantaClient, "_post", forbidden)


def test_env_file_credentials_with_inline_test_environment_are_blocked(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    # Reproduces the incident: only some settings were overridden inline while
    # provider credentials were still loaded from the repository env file.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENVIRONMENT=development\n"
        + "".join(f"APP_{name.upper()}={value}\n" for name, value in PROVIDER_CREDENTIALS.items())
    )
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    settings = Settings(_env_file=env_file)

    assert settings.environment is Environment.TEST
    assert settings.whatsapp_access_token is not None
    assert settings.advanta_api_key is not None
    assert settings.live_provider_sends_allowed is False
    assert build_provider_clients(settings) == provider_egress.ProviderClients(None, None)


def test_development_with_credentials_and_no_opt_in_is_blocked(provider_tripwire) -> None:
    settings = settings_for(Environment.DEVELOPMENT)

    assert settings.live_provider_sends_allowed is False
    clients = build_provider_clients(settings)
    assert clients.whatsapp is None
    assert clients.advanta is None
    with pytest.raises(LiveProviderEgressBlockedError, match="development"):
        require_live_provider_egress(settings)


@pytest.mark.parametrize("opt_in", [None, False])
def test_test_environment_with_credentials_is_blocked(opt_in) -> None:
    settings = settings_for(Environment.TEST, allow_live_provider_sends=opt_in)

    assert settings.live_provider_sends_allowed is False
    assert build_provider_clients(settings) == provider_egress.ProviderClients(None, None)


def test_test_environment_rejects_live_send_opt_in() -> None:
    with pytest.raises(ValidationError, match="cannot be enabled in the test environment"):
        settings_for(Environment.TEST, allow_live_provider_sends=True)


def test_opt_in_setting_is_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("APP_ALLOW_LIVE_PROVIDER_SENDS", "true")

    settings = Settings(_env_file=None, environment=Environment.DEVELOPMENT)

    assert settings.live_provider_sends_allowed is True


@pytest.mark.asyncio
async def test_development_explicit_opt_in_invokes_provider_client(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.mocked"}]})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = settings_for(Environment.DEVELOPMENT, allow_live_provider_sends=True)

    clients = build_provider_clients(settings)
    result = await clients.whatsapp.send_text_message("254700000001", "hello")

    assert isinstance(clients.advanta, AdvantaClient)
    assert result.meta_message_id == "wamid.mocked"
    assert len(requests) == 1
    assert requests[0].url.path == "/v99.0/000000000/messages"
    assert json.loads(requests[0].content)["to"] == "254700000001"


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.STAGING])
def test_production_configuration_keeps_live_providers(environment) -> None:
    settings = settings_for(environment)

    assert settings.allow_live_provider_sends is None
    assert settings.live_provider_sends_allowed is True
    clients = build_provider_clients(settings)
    assert isinstance(clients.whatsapp, WhatsAppCloudAPIClient)
    assert isinstance(clients.advanta, AdvantaClient)
    require_live_provider_egress(settings)


def test_production_can_explicitly_disable_live_providers() -> None:
    settings = settings_for(Environment.PRODUCTION, allow_live_provider_sends=False)

    assert settings.live_provider_sends_allowed is False
    assert build_provider_clients(settings) == provider_egress.ProviderClients(None, None)


def test_http_messaging_service_uses_guarded_clients() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings_for(Environment.DEVELOPMENT)))
    )

    service = get_messaging_service(request)

    assert service.client is None
    assert service.advanta_client is None


@pytest.mark.asyncio
async def test_http_send_is_blocked_in_development_despite_credentials(provider_tripwire) -> None:
    class Session:
        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def scalar(self, statement):
            return None

    async def session_override():
        yield Session()

    app = create_app(settings_for(Environment.DEVELOPMENT))
    app.dependency_overrides[get_authenticated_application] = lambda: SimpleNamespace(
        id=uuid4(), slug="egress-test-app", billing_required=False
    )
    app.dependency_overrides[get_db_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/messages/text",
            json={"channel": "whatsapp", "to": "254700000001", "text": "hello"},
            headers={"Idempotency-Key": "egress-guard-0001"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Message provider is not configured"}


def test_cli_messaging_service_uses_guarded_clients() -> None:
    service = cli._messaging_service_from_settings(
        settings_for(Environment.DEVELOPMENT), claim_identity="sweeper-test"
    )

    assert service.client is None
    assert service.advanta_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
async def test_cli_dispatch_sweep_refuses_without_touching_database(
    monkeypatch, environment
) -> None:
    def forbidden_session():
        raise AssertionError("blocked sweep must not open a database session")

    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(environment))
    monkeypatch.setattr(cli, "async_session_factory", forbidden_session)

    with pytest.raises(SystemExit, match="Live provider egress is disabled"):
        await cli.dispatch_sweep(argparse.Namespace())


@pytest.mark.asyncio
async def test_cli_advanta_balance_is_blocked_outside_production(
    monkeypatch, provider_tripwire
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(Environment.DEVELOPMENT))

    with pytest.raises(SystemExit, match="Live provider egress is disabled"):
        await cli.advanta_balance(argparse.Namespace())


def test_provider_clients_are_only_constructed_by_the_egress_guard() -> None:
    app_root = pathlib.Path(__file__).resolve().parents[1] / "app"
    constructors = ("WhatsAppCloudAPIClient(", "AdvantaClient(")
    offenders = [
        str(path.relative_to(app_root))
        for path in app_root.rglob("*.py")
        if path.name not in {"provider_egress.py", "whatsapp_client.py", "advanta_client.py"}
        and any(constructor in path.read_text() for constructor in constructors)
    ]

    assert offenders == []
