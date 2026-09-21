from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.advanta_webhook import AdvantaWebhookService


class Session:
    def __init__(self, message):
        self.message = message

    @asynccontextmanager
    async def begin(self):
        yield

    async def scalar(self, statement):
        return self.message


def message(status="sent"):
    return SimpleNamespace(
        status=status,
        sent_at=None,
        delivered_at=None,
        failed_at=None,
        error_code=None,
        error_message=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "status"),
    [
        ("DeliveredToTerminal", "delivered"),
        ("SentToNetwork", "sent"),
        ("AbsentSubscriber", "failed"),
        ("DeliveryImpossible", "failed"),
        ("SenderName Blacklisted", "failed"),
    ],
)
async def test_advanta_status_mapping(description: str, status: str) -> None:
    outbound = message("pending")
    await AdvantaWebhookService().accept(
        Session(outbound), {"messageid": "adv-1", "description": description}
    )
    assert outbound.status == status


@pytest.mark.asyncio
async def test_unknown_duplicate_missing_and_malformed_callbacks_are_safe() -> None:
    outbound = message("delivered")
    service = AdvantaWebhookService()
    await service.accept(
        Session(outbound), {"messageid": "adv-1", "description": "DeliveredToTerminal"}
    )
    await service.accept(Session(outbound), {"messageid": "adv-1", "description": "Unknown"})
    await service.accept(Session(None), {"messageid": "missing", "description": "SentToNetwork"})
    await service.accept(Session(outbound), {"bad": "payload"})
    assert outbound.status == "delivered"
