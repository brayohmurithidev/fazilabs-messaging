from uuid import uuid4

import pytest

from app.api.routes.whatsapp import get_webhook_service
from app.core.config import Environment, Settings
from app.db.session import get_db_session
from app.main import create_app
from app.services.whatsapp_webhook import WhatsAppWebhookService


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class FakeSession:
    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    async def scalar(self, statement):
        return None


class FakeWhatsAppMessageRepository:
    def __init__(self) -> None:
        self.messages = {}

    async def insert_inbound_messages(self, session, messages):
        inserted = {}
        for message in messages:
            if message.meta_message_id not in self.messages:
                self.messages[message.meta_message_id] = message
                inserted[message.meta_message_id] = uuid4()
        return inserted


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_name="Test Messaging Platform",
        environment=Environment.TEST,
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_agent_test",
        whatsapp_verify_token="test-verify-token",
        whatsapp_app_secret="test-app-secret",
        whatsapp_access_token="test-access-token",
        whatsapp_phone_number_id="123456789",
        whatsapp_api_version="vXX.X",
    )


@pytest.fixture
def message_repository() -> FakeWhatsAppMessageRepository:
    return FakeWhatsAppMessageRepository()


@pytest.fixture
def app(test_settings: Settings, message_repository: FakeWhatsAppMessageRepository):
    application = create_app(test_settings)
    service = WhatsAppWebhookService(message_repository)

    async def override_session():
        yield FakeSession()

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_webhook_service] = lambda: service
    return application
