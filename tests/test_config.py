import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings


def test_configuration_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "Environment App")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/environment_test",
    )

    settings = Settings(_env_file=None)

    assert settings.app_name == "Environment App"
    assert settings.environment is Environment.TEST
    assert settings.port == 9000
    assert settings.database_url is not None
    assert settings.database_url.path == "/environment_test"


def test_production_requires_database_url(monkeypatch) -> None:
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="APP_DATABASE_URL is required"):
        Settings(_env_file=None, environment=Environment.PRODUCTION)


def test_production_rejects_debug_mode() -> None:
    with pytest.raises(ValidationError, match="APP_DEBUG must be false"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            debug=True,
            database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/production",
        )


def test_production_requires_whatsapp_configuration() -> None:
    with pytest.raises(ValidationError, match="APP_WHATSAPP_APP_SECRET"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/production",
        )


def test_whatsapp_secrets_are_masked() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        whatsapp_verify_token="verify-secret",
        whatsapp_app_secret="app-secret",
        whatsapp_access_token="access-secret",
    )
    rendered = repr(settings)
    assert "verify-secret" not in rendered
    assert "app-secret" not in rendered
    assert "access-secret" not in rendered


def test_public_webhook_url_is_optional() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST)
    assert settings.public_webhook_base_url is None
