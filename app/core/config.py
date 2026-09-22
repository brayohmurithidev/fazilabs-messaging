from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Environment-driven application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="Fazilabs Messaging Platform", validation_alias="APP_NAME")
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    database_url: PostgresDsn | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_api_version: str | None = None
    public_webhook_base_url: str | None = None
    advanta_base_url: str | None = None
    advanta_api_key: SecretStr | None = None
    advanta_partner_id: SecretStr | None = None
    advanta_sender_id: str = "FAZILABS"
    advanta_provider_cost_per_page_minor: int | None = Field(default=None, ge=0)
    billing_uncertain_reconcile_after_minutes: int = Field(default=1440, ge=1)
    dispatch_claim_lease_seconds: int = Field(default=90, ge=1)
    dispatch_pending_grace_seconds: int = Field(default=60, ge=1)
    dispatch_sweep_batch_size: int = Field(default=25, ge=1)

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if self.environment in {Environment.STAGING, Environment.PRODUCTION}:
            if self.database_url is None:
                raise ValueError("APP_DATABASE_URL is required in staging and production")
            if self.debug:
                raise ValueError("APP_DEBUG must be false in staging and production")
            missing_whatsapp_settings = [
                name
                for name, value in {
                    "APP_WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token,
                    "APP_WHATSAPP_APP_SECRET": self.whatsapp_app_secret,
                    "APP_WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token,
                    "APP_WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
                    "APP_WHATSAPP_API_VERSION": self.whatsapp_api_version,
                }.items()
                if value is None
            ]
            if missing_whatsapp_settings:
                missing = ", ".join(missing_whatsapp_settings)
                raise ValueError(f"Missing required WhatsApp configuration: {missing}")
        if self.database_url is None:
            database = (
                "whatsapp_agent_test" if self.environment is Environment.TEST else "whatsapp_agent"
            )
            self.database_url = PostgresDsn(
                f"postgresql+asyncpg://postgres:postgres@localhost:5432/{database}"
            )

        self.log_level = self.log_level.upper()
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
