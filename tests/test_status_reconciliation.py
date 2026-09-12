from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.whatsapp_webhook import WhatsAppWebhookService, extract_status_events


class StatusSession:
    def __init__(self, message):
        self.message = message

    async def scalar(self, statement):
        return self.message


def message(status="pending"):
    return SimpleNamespace(
        status=status,
        sent_at=None,
        delivered_at=None,
        read_at=None,
        failed_at=None,
        error_code=None,
        error_message=None,
    )


def event(status: str, timestamp="1720000000") -> dict:
    return {"id": "wamid.outbound", "status": status, "timestamp": timestamp}


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["sent", "delivered", "read"])
async def test_status_progresses(new_status: str) -> None:
    outbound = message()
    await WhatsAppWebhookService()._reconcile_statuses(StatusSession(outbound), [event(new_status)])
    assert outbound.status == new_status
    assert getattr(outbound, f"{new_status}_at") == datetime.fromtimestamp(1720000000, tz=UTC)


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_statuses_do_not_regress() -> None:
    outbound = message("delivered")
    outbound.delivered_at = datetime.now(UTC)
    original = outbound.delivered_at
    service = WhatsAppWebhookService()
    await service._reconcile_statuses(StatusSession(outbound), [event("sent"), event("delivered")])
    assert outbound.status == "delivered"
    assert outbound.delivered_at == original


@pytest.mark.asyncio
async def test_sent_template_progresses_through_delivered_to_read() -> None:
    outbound = message("sent")
    service = WhatsAppWebhookService()
    session = StatusSession(outbound)
    await service._reconcile_statuses(session, [event("delivered")])
    assert outbound.status == "delivered"
    assert outbound.delivered_at is not None
    await service._reconcile_statuses(session, [event("read", "1720000010")])
    assert outbound.status == "read"
    assert outbound.read_at == datetime.fromtimestamp(1720000010, tz=UTC)


@pytest.mark.asyncio
async def test_failed_is_terminal_and_sanitized() -> None:
    outbound = message()
    failed = event("failed") | {"errors": [{"code": 131000, "title": "sensitive"}]}
    service = WhatsAppWebhookService()
    await service._reconcile_statuses(StatusSession(outbound), [failed, event("sent")])
    assert outbound.status == "failed"
    assert outbound.error_code == "131000"
    assert outbound.error_message == "WhatsApp delivery failed"
    assert outbound.failed_at is not None


@pytest.mark.asyncio
async def test_unknown_provider_id_is_safe() -> None:
    await WhatsAppWebhookService()._reconcile_statuses(StatusSession(None), [event("read")])


def test_status_extraction_ignores_malformed_entries() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [event("sent"), {"status": "read"}, event("unknown")]
                        },
                    }
                ]
            }
        ],
    }
    assert extract_status_events(payload) == [event("sent")]
