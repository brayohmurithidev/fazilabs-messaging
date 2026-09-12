from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.whatsapp_outbound_message import OutboundMessageStatus
from app.schemas.messages import TemplateMessageRequest, TextMessageRequest
from app.schemas.whatsapp import WhatsAppSendResult
from app.services.billing import BillingQuote, InsufficientBalanceError
from app.services.messaging import (
    IdempotencyConflictError,
    MessagingService,
    TemplateNotFoundError,
    TemplateParameterError,
    TemplateUnavailableError,
    canonical_payload_hash,
)
from app.services.whatsapp_client import WhatsAppClientResponseError, WhatsAppTimeoutError


class Session:
    @asynccontextmanager
    async def begin(self):
        yield

    async def refresh(self, message):
        pass


class Repository:
    def __init__(self, existing=None):
        self.existing = existing
        self.message = None

    async def get_by_idempotency_key(self, session, application_id, idempotency_key):
        return self.existing

    async def reserve(self, session, **values):
        self.message = SimpleNamespace(
            id=uuid4(),
            payload_hash=values["payload_hash"],
            status="pending",
            provider_message_id=None,
            billing_account_id=values.get("billing_account_id"),
        )
        return self.message, True

    async def mark_sent(self, session, message_id, provider_message_id, timestamp):
        self.message.status = "sent"
        self.message.provider_message_id = provider_message_id

    async def mark_error(
        self, session, message_id, *, status, error_code, error_message, timestamp
    ):
        self.message.status = status
        self.message.error_code = error_code
        self.message.error_message = error_message


class Provider:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    async def send_text_message(self, recipient, text):
        self.calls += 1
        if self.error:
            raise self.error
        return WhatsAppSendResult(recipient=recipient, meta_message_id="wamid.sent", success=True)

    async def send_template_message(self, recipient, **kwargs):
        self.calls += 1
        self.template_call = (recipient, kwargs)
        if self.error:
            raise self.error
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id="wamid.template", success=True
        )


class TemplateRepository:
    def __init__(self, template):
        self.template = template

    async def get(self, session, application_id, key, channel):
        return self.template


class Billing:
    def __init__(self, error=None):
        self.error = error
        self.reservations = 0
        self.charges = 0
        self.releases = 0

    async def quote_and_lock(self, session, **kwargs):
        if self.error:
            raise self.error
        return BillingQuote(
            account=SimpleNamespace(id=uuid4()),
            pricing_rule=SimpleNamespace(id=uuid4(), customer_price_minor=100, currency="KES"),
        )

    async def reserve(self, session, quote, outbound_message_id):
        self.reservations += 1

    async def charge(self, session, message, timestamp):
        self.charges += 1

    async def release(self, session, outbound_message_id):
        self.releases += 1


def request(text="hello"):
    return TextMessageRequest(channel="whatsapp", to="254700000001", text=text)


def template_request(**changes):
    values = {
        "channel": "whatsapp",
        "to": "254700000001",
        "template": "student_results_ready",
        "parameters": {"parent_name": "Jane", "student_name": "Brian", "term": "Term 2"},
    }
    values.update(changes)
    return TemplateMessageRequest(**values)


def template(**changes):
    values = {
        "status": "active",
        "provider": "meta",
        "provider_template_name": "fazi_student_results_ready_v1",
        "language_code": "en_US",
        "template_key": "student_results_ready",
        "parameter_schema": {"body": ["parent_name", "student_name", "term"]},
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_first_request_sends_once_and_marks_sent() -> None:
    provider = Provider()
    repository = Repository()
    service = MessagingService(provider, repository)
    result = await service.send_text(
        Session(), SimpleNamespace(id=uuid4()), "test-key-0001", request()
    )
    assert provider.calls == 1
    assert result.status == "sent"
    assert result.provider_message_id == "wamid.sent"


@pytest.mark.asyncio
async def test_exact_retry_returns_existing_without_provider_call() -> None:
    payload = request()
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(payload), status="sent")
    provider = Provider()
    result = await MessagingService(provider, Repository(existing)).send_text(
        Session(), SimpleNamespace(id=uuid4()), "test-key-0001", payload
    )
    assert result is existing
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_same_key_with_different_payload_conflicts() -> None:
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(request("first")))
    with pytest.raises(IdempotencyConflictError):
        await MessagingService(Provider(), Repository(existing)).send_text(
            Session(), SimpleNamespace(id=uuid4()), "test-key-0001", request("second")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WhatsAppClientResponseError("bad request", status_code=400), OutboundMessageStatus.FAILED),
        (
            WhatsAppClientResponseError("server error", status_code=503),
            OutboundMessageStatus.FAILED,
        ),
        (WhatsAppTimeoutError("timeout", ambiguous_delivery=True), OutboundMessageStatus.UNCERTAIN),
    ],
)
async def test_provider_failures_are_persisted(error, expected) -> None:
    repository = Repository()
    result = await MessagingService(Provider(error), repository).send_text(
        Session(), SimpleNamespace(id=uuid4()), "test-key-0001", request()
    )
    assert result.status == expected


@pytest.mark.asyncio
async def test_valid_template_reserves_snapshot_and_sends_once() -> None:
    provider = Provider()
    repository = Repository()
    service = MessagingService(provider, repository, TemplateRepository(template()))
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "template-key-001", template_request()
    )
    assert result.status == "sent"
    assert provider.calls == 1
    assert provider.template_call[1]["body_parameters"] == ["Jane", "Brian", "Term 2"]


@pytest.mark.asyncio
async def test_template_retry_does_not_call_meta_twice() -> None:
    payload = template_request()
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(payload), status="sent")
    provider = Provider()
    service = MessagingService(provider, Repository(existing), TemplateRepository(template()))
    assert (
        await service.send_template(
            Session(), SimpleNamespace(id=uuid4()), "template-key-001", payload
        )
        is existing
    )
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"to": "254700000002"},
        {"template": "fee_payment_reminder"},
        {
            "parameters": {
                "parent_name": "Janet",
                "student_name": "Brian",
                "term": "Term 2",
            }
        },
    ],
)
async def test_template_idempotency_conflicts_for_changed_identity(changed) -> None:
    original = template_request()
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(original))
    requested = template_request(**changed)
    mapped = template(template_key=requested.template)
    service = MessagingService(Provider(), Repository(existing), TemplateRepository(mapped))
    with pytest.raises(IdempotencyConflictError):
        await service.send_template(
            Session(), SimpleNamespace(id=uuid4()), "template-key-001", requested
        )


@pytest.mark.asyncio
async def test_template_not_found_disabled_and_invalid_parameters() -> None:
    application = SimpleNamespace(id=uuid4())
    with pytest.raises(TemplateNotFoundError):
        await MessagingService(Provider(), Repository(), TemplateRepository(None)).send_template(
            Session(), application, "template-key-001", template_request()
        )
    with pytest.raises(TemplateUnavailableError):
        await MessagingService(
            Provider(), Repository(), TemplateRepository(template(status="disabled"))
        ).send_template(Session(), application, "template-key-001", template_request())
    with pytest.raises(TemplateParameterError, match="missing parameters: term"):
        await MessagingService(
            Provider(), Repository(), TemplateRepository(template())
        ).send_template(
            Session(),
            application,
            "template-key-001",
            template_request(parameters={"parent_name": "Jane", "student_name": "Brian"}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WhatsAppClientResponseError("bad request", status_code=400), OutboundMessageStatus.FAILED),
        (WhatsAppTimeoutError("timeout", ambiguous_delivery=True), OutboundMessageStatus.UNCERTAIN),
    ],
)
async def test_template_provider_failures_use_existing_lifecycle(error, expected) -> None:
    repository = Repository()
    service = MessagingService(Provider(error), repository, TemplateRepository(template()))
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "template-key-001", template_request()
    )
    assert result.status == expected


@pytest.mark.asyncio
async def test_billed_send_reserves_then_charges_once() -> None:
    provider = Provider()
    billing = Billing()
    repository = Repository()
    result = await MessagingService(provider, repository, billing_service=billing).send_text(
        Session(),
        SimpleNamespace(id=uuid4()),
        "billing-key-001",
        request().model_copy(update={"billing_account": "school-001"}),
    )
    assert result.status == "sent"
    assert (provider.calls, billing.reservations, billing.charges, billing.releases) == (1, 1, 1, 0)


@pytest.mark.asyncio
async def test_insufficient_balance_never_calls_provider() -> None:
    provider = Provider()
    billing = Billing(InsufficientBalanceError())
    with pytest.raises(InsufficientBalanceError):
        await MessagingService(provider, Repository(), billing_service=billing).send_text(
            Session(),
            SimpleNamespace(id=uuid4()),
            "billing-key-002",
            request().model_copy(update={"billing_account": "school-001"}),
        )
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_billed_exact_retry_does_not_touch_billing_or_provider() -> None:
    payload = request().model_copy(update={"billing_account": "school-001"})
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(payload), status="sent")
    provider = Provider()
    billing = Billing(AssertionError("billing must not run for an exact retry"))
    result = await MessagingService(
        provider, Repository(existing), billing_service=billing
    ).send_text(Session(), SimpleNamespace(id=uuid4()), "billing-key-005", payload)
    assert result is existing
    assert provider.calls == 0
    assert (billing.reservations, billing.charges, billing.releases) == (0, 0, 0)


@pytest.mark.asyncio
async def test_confirmed_rejection_releases_but_timeout_retains_reservation() -> None:
    rejected_billing = Billing()
    await MessagingService(
        Provider(WhatsAppClientResponseError("rejected", status_code=400)),
        Repository(),
        billing_service=rejected_billing,
    ).send_text(
        Session(),
        SimpleNamespace(id=uuid4()),
        "billing-key-003",
        request().model_copy(update={"billing_account": "school-001"}),
    )
    assert (rejected_billing.charges, rejected_billing.releases) == (0, 1)

    uncertain_billing = Billing()
    result = await MessagingService(
        Provider(WhatsAppTimeoutError("timeout", ambiguous_delivery=True)),
        Repository(),
        billing_service=uncertain_billing,
    ).send_text(
        Session(),
        SimpleNamespace(id=uuid4()),
        "billing-key-004",
        request().model_copy(update={"billing_account": "school-001"}),
    )
    assert result.status == OutboundMessageStatus.UNCERTAIN
    assert (uncertain_billing.charges, uncertain_billing.releases) == (0, 0)
