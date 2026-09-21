from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.whatsapp_outbound_message import OutboundMessageStatus
from app.schemas.messages import TemplateMessageRequest, TextMessageRequest
from app.schemas.whatsapp import WhatsAppSendResult
from app.services.advanta_client import AdvantaAPIError, AdvantaResult
from app.services.billing import BillingAccountNotFoundError, BillingQuote, InsufficientBalanceError
from app.services.messaging import (
    BillingAccountRequiredError,
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
            billing_mode=values.get("billing_mode", "customer"),
            application_id=values.get("application_id"),
            channel=values.get("channel"),
            message_kind=values.get("message_kind"),
            billing_category=values.get("billing_category"),
            provider=values.get("provider"),
            text_body=values.get("text_body"),
            provider_route=values.get("provider_route"),
            sms_character_count=values.get("sms_character_count"),
            sms_page_count=values.get("sms_page_count"),
            provider_cost_minor=values.get("provider_cost_minor"),
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
        self.platform_usage = 0
        self.attributed_account = SimpleNamespace(id=uuid4())
        self.quote_kwargs = None

    async def quote_and_lock(self, session, **kwargs):
        self.quote_kwargs = kwargs
        if self.error:
            raise self.error
        return BillingQuote(
            account=SimpleNamespace(id=uuid4()),
            pricing_rule=SimpleNamespace(id=uuid4(), customer_price_minor=100, currency="KES"),
            amount_minor=100 * kwargs.get("units", 1),
        )

    async def reserve(self, session, quote, outbound_message_id):
        self.reservations += 1

    async def resolve_account(self, session, **kwargs):
        if self.error:
            raise self.error
        return self.attributed_account

    async def charge(self, session, message, timestamp):
        self.charges += 1

    async def release(self, session, outbound_message_id):
        self.releases += 1

    async def record_platform_usage(self, session, message):
        self.platform_usage += 1


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
        "billing_category": "utility",
        "billing_mode": "customer",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def dynamic_template_request(**changes):
    values = {
        "parameters": {
            "parent_name": "Amina",
            "student_name": "Baraka",
            "term": "Term 2",
            "results_path": "results/opaque-token",
        }
    }
    values.update(changes)
    return template_request(**values)


def dynamic_template(**changes):
    values = {
        "parameter_schema": {
            "body": ["parent_name", "student_name", "term"],
            "buttons": [{"index": 0, "type": "url", "parameter": "results_path"}],
        }
    }
    values.update(changes)
    return template(**values)


class AdvantaProvider:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return AdvantaResult("advanta-message-1")


def sms_template(**changes):
    values = {
        "status": "active",
        "provider": "advanta",
        "provider_template_name": None,
        "language_code": None,
        "template_key": "generic_notice",
        "parameter_schema": {"body": ["content"], "buttons": []},
        "billing_category": "utility",
        "billing_mode": "customer",
        "sms_body": "{{content}}",
        "provider_route": "standard",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def sms_request(**changes):
    values = {
        "channel": "sms",
        "to": "0712345678",
        "template": "generic_notice",
        "parameters": {"content": "Hello"},
        "billing_account": "demo-account",
    }
    values.update(changes)
    return TemplateMessageRequest(**values)


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
async def test_required_billing_rejects_new_text_before_row_or_provider() -> None:
    provider = Provider()
    repository = Repository()
    billing = Billing()
    with pytest.raises(BillingAccountRequiredError):
        await MessagingService(provider, repository, billing_service=billing).send_text(
            Session(),
            SimpleNamespace(id=uuid4(), billing_required=True),
            "billing-required-text-001",
            request(),
        )
    assert repository.message is None
    assert provider.calls == 0
    assert billing.reservations == 0


@pytest.mark.asyncio
async def test_required_billing_rejects_new_template_before_lookup_row_or_provider() -> None:
    provider = Provider()
    repository = Repository()
    billing = Billing()
    with pytest.raises(BillingAccountRequiredError):
        await MessagingService(
            provider, repository, TemplateRepository(template()), billing_service=billing
        ).send_template(
            Session(),
            SimpleNamespace(id=uuid4(), billing_required=True),
            "billing-required-template-001",
            template_request(),
        )
    assert repository.message is None
    assert provider.calls == 0
    assert billing.reservations == 0


@pytest.mark.asyncio
async def test_required_billing_preserves_exact_historical_unbilled_replay() -> None:
    payload = request()
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(payload), status="uncertain")
    provider = Provider()
    result = await MessagingService(provider, Repository(existing)).send_text(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "historical-unbilled-001",
        payload,
    )
    assert result is existing
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_non_required_application_still_allows_unbilled_send() -> None:
    provider = Provider()
    repository = Repository()
    result = await MessagingService(provider, repository).send_text(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=False),
        "legacy-unbilled-001",
        request(),
    )
    assert result.status == "sent"
    assert repository.message.billing_account_id is None


@pytest.mark.asyncio
async def test_required_application_with_account_uses_existing_billing_flow() -> None:
    provider = Provider()
    billing = Billing()
    result = await MessagingService(provider, Repository(), billing_service=billing).send_text(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "required-billed-001",
        request().model_copy(update={"billing_account": "school-001"}),
    )
    assert result.status == "sent"
    assert (provider.calls, billing.reservations, billing.charges) == (1, 1, 1)


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
            WhatsAppClientResponseError("server error", status_code=503, ambiguous_delivery=True),
            OutboundMessageStatus.UNCERTAIN,
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
async def test_dynamic_url_template_passes_ordered_semantic_values_to_provider() -> None:
    provider = Provider()
    service = MessagingService(provider, Repository(), TemplateRepository(dynamic_template()))
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "dynamic-template-001", dynamic_template_request()
    )
    assert result.status == "sent"
    assert provider.template_call[1]["body_parameters"] == ["Amina", "Baraka", "Term 2"]
    assert [
        (button.index, button.value)
        for button in provider.template_call[1]["url_button_parameters"]
    ] == [(0, "results/opaque-token")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        (
            {"parent_name": "Amina", "student_name": "Baraka", "term": "Term 2"},
            "missing parameters: results_path",
        ),
        (
            {
                "parent_name": "Amina",
                "student_name": "Baraka",
                "term": "Term 2",
                "results_path": "results/token",
                "components": "raw-provider-structure",
            },
            "unexpected parameters: components",
        ),
    ],
)
async def test_dynamic_url_template_rejects_missing_and_extra_parameters(
    parameters, message
) -> None:
    provider = Provider()
    service = MessagingService(provider, Repository(), TemplateRepository(dynamic_template()))
    with pytest.raises(TemplateParameterError, match=message):
        await service.send_template(
            Session(),
            SimpleNamespace(id=uuid4()),
            "dynamic-template-invalid-001",
            dynamic_template_request(parameters=parameters),
        )
    assert provider.calls == 0


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
async def test_dynamic_button_exact_retry_does_not_call_provider_or_bill_again() -> None:
    payload = dynamic_template_request(billing_account="school-001")
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(payload), status="sent")
    provider = Provider()
    billing = Billing(AssertionError("billing must not run for an exact retry"))
    service = MessagingService(
        provider,
        Repository(existing),
        TemplateRepository(dynamic_template()),
        billing_service=billing,
    )
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "dynamic-template-retry-001", payload
    )
    assert result is existing
    assert provider.calls == 0
    assert (billing.reservations, billing.charges, billing.releases) == (0, 0, 0)


@pytest.mark.asyncio
async def test_changed_dynamic_button_value_conflicts_for_same_idempotency_key() -> None:
    original = dynamic_template_request()
    existing = SimpleNamespace(payload_hash=canonical_payload_hash(original), status="sent")
    changed_parameters = {**original.parameters, "results_path": "results/different-token"}
    service = MessagingService(
        Provider(), Repository(existing), TemplateRepository(dynamic_template())
    )
    with pytest.raises(IdempotencyConflictError):
        await service.send_template(
            Session(),
            SimpleNamespace(id=uuid4()),
            "dynamic-template-retry-002",
            dynamic_template_request(parameters=changed_parameters),
        )


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
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_releases"),
    [
        (
            WhatsAppClientResponseError("rejected", status_code=400),
            OutboundMessageStatus.FAILED,
            1,
        ),
        (
            WhatsAppTimeoutError("timeout", ambiguous_delivery=True),
            OutboundMessageStatus.UNCERTAIN,
            0,
        ),
    ],
)
async def test_billed_dynamic_template_preserves_failure_financial_semantics(
    error, expected_status, expected_releases
) -> None:
    billing = Billing()
    result = await MessagingService(
        Provider(error),
        Repository(),
        TemplateRepository(dynamic_template()),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4()),
        "dynamic-template-billing-001",
        dynamic_template_request(billing_account="school-001"),
    )
    assert result.status == expected_status
    assert (billing.reservations, billing.charges, billing.releases) == (
        1,
        0,
        expected_releases,
    )


@pytest.mark.asyncio
async def test_successful_billed_dynamic_template_reserves_and_charges_once() -> None:
    provider = Provider()
    billing = Billing()
    result = await MessagingService(
        provider,
        Repository(),
        TemplateRepository(dynamic_template()),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4()),
        "dynamic-template-billing-success-001",
        dynamic_template_request(billing_account="school-001"),
    )
    assert result.status == OutboundMessageStatus.SENT
    assert provider.calls == 1
    assert (billing.reservations, billing.charges, billing.releases) == (1, 1, 0)


@pytest.mark.asyncio
async def test_platform_template_without_account_tracks_usage_without_customer_charge() -> None:
    provider = Provider()
    billing = Billing()
    repository = Repository()
    result = await MessagingService(
        provider,
        repository,
        TemplateRepository(dynamic_template(billing_mode="platform")),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "platform-template-001",
        dynamic_template_request(),
    )
    assert result.status == OutboundMessageStatus.SENT
    assert result.billing_mode == "platform"
    assert result.billing_account_id is None
    assert provider.calls == 1
    assert (billing.reservations, billing.charges, billing.releases, billing.platform_usage) == (
        0,
        0,
        0,
        1,
    )


@pytest.mark.asyncio
async def test_platform_template_account_is_attribution_only() -> None:
    provider = Provider()
    billing = Billing()
    repository = Repository()
    result = await MessagingService(
        provider,
        repository,
        TemplateRepository(dynamic_template(billing_mode="platform")),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "platform-template-attributed-001",
        dynamic_template_request(billing_account="school-001"),
    )
    assert result.billing_account_id == billing.attributed_account.id
    assert (billing.reservations, billing.charges, billing.releases, billing.platform_usage) == (
        0,
        0,
        0,
        1,
    )


@pytest.mark.asyncio
async def test_platform_template_rejects_inaccessible_attribution_account_before_provider() -> None:
    provider = Provider()
    billing = Billing(BillingAccountNotFoundError())
    service = MessagingService(
        provider,
        Repository(),
        TemplateRepository(dynamic_template(billing_mode="platform")),
        billing_service=billing,
    )
    with pytest.raises(BillingAccountNotFoundError):
        await service.send_template(
            Session(),
            SimpleNamespace(id=uuid4()),
            "platform-template-account-error-001",
            dynamic_template_request(billing_account="other-account"),
        )
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            WhatsAppClientResponseError("rejected", status_code=400),
            OutboundMessageStatus.FAILED,
        ),
        (
            WhatsAppTimeoutError("timeout", ambiguous_delivery=True),
            OutboundMessageStatus.UNCERTAIN,
        ),
    ],
)
async def test_platform_template_provider_failures_never_touch_customer_wallet(
    error, expected_status
) -> None:
    billing = Billing()
    result = await MessagingService(
        Provider(error),
        Repository(),
        TemplateRepository(dynamic_template(billing_mode="platform")),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4()),
        "platform-template-failure-001",
        dynamic_template_request(),
    )
    assert result.status == expected_status
    assert (billing.reservations, billing.charges, billing.releases, billing.platform_usage) == (
        0,
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_platform_template_exact_replay_preserves_original_funding_snapshot() -> None:
    payload = dynamic_template_request()
    existing = SimpleNamespace(
        payload_hash=canonical_payload_hash(payload), status="sent", billing_mode="platform"
    )
    provider = Provider()
    billing = Billing(AssertionError("billing must not run for an exact retry"))
    result = await MessagingService(
        provider,
        Repository(existing),
        TemplateRepository(dynamic_template(billing_mode="customer")),
        billing_service=billing,
    ).send_template(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "platform-replay-001",
        payload,
    )
    assert result is existing
    assert result.billing_mode == "platform"
    assert provider.calls == 0
    assert (billing.reservations, billing.charges, billing.releases, billing.platform_usage) == (
        0,
        0,
        0,
        0,
    )


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


@pytest.mark.asyncio
async def test_customer_sms_is_rendered_analyzed_priced_per_page_and_charged() -> None:
    provider, billing, repository = AdvantaProvider(), Billing(), Repository()
    content = "x" * 161
    service = MessagingService(
        Provider(),
        repository,
        TemplateRepository(sms_template()),
        billing,
        advanta_client=provider,
        advanta_provider_cost_per_page_minor=20,
    )
    result = await service.send_template(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "sms-customer-001",
        sms_request(parameters={"content": content}),
    )
    assert (result.sms_page_count, result.sms_character_count, result.provider_cost_minor) == (
        2,
        161,
        40,
    )
    assert result.text_body == content
    assert billing.quote_kwargs["units"] == 2
    assert billing.reservations == billing.charges == 1
    assert provider.calls == [{"mobile": "254712345678", "message": content, "route": "standard"}]


@pytest.mark.asyncio
async def test_platform_sms_tracks_usage_without_customer_debit() -> None:
    provider, billing = AdvantaProvider(), Billing()
    service = MessagingService(
        Provider(),
        Repository(),
        TemplateRepository(sms_template(billing_mode="platform", provider_route="transactional")),
        billing,
        advanta_client=provider,
    )
    result = await service.send_template(
        Session(),
        SimpleNamespace(id=uuid4(), billing_required=True),
        "sms-platform-001",
        sms_request(billing_account=None),
    )
    assert result.provider_cost_minor is None
    assert billing.reservations == billing.charges == 0
    assert billing.platform_usage == 1
    assert provider.calls[0]["route"] == "transactional"


@pytest.mark.asyncio
async def test_platform_sms_account_is_attribution_only() -> None:
    provider, billing = AdvantaProvider(), Billing()
    service = MessagingService(
        Provider(),
        Repository(),
        TemplateRepository(sms_template(billing_mode="platform")),
        billing,
        advanta_client=provider,
    )
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "sms-attribution-001", sms_request()
    )
    assert result.billing_account_id == billing.attributed_account.id
    assert billing.reservations == billing.charges == 0


@pytest.mark.asyncio
async def test_invalid_sms_stops_before_billing_and_provider() -> None:
    provider, billing = AdvantaProvider(), Billing()
    service = MessagingService(
        Provider(),
        Repository(),
        TemplateRepository(sms_template()),
        billing,
        advanta_client=provider,
    )
    with pytest.raises(TemplateParameterError, match="emoji"):
        await service.send_template(
            Session(),
            SimpleNamespace(id=uuid4()),
            "sms-invalid-001",
            sms_request(parameters={"content": "No emoji 😊"}),
        )
    assert billing.quote_kwargs is None
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ambiguous", "status", "releases"), [(False, "failed", 1), (True, "uncertain", 0)]
)
async def test_sms_provider_failure_preserves_financial_semantics(
    ambiguous: bool, status: str, releases: int
) -> None:
    provider = AdvantaProvider(AdvantaAPIError("safe error", ambiguous_delivery=ambiguous))
    billing = Billing()
    service = MessagingService(
        Provider(),
        Repository(),
        TemplateRepository(sms_template()),
        billing,
        advanta_client=provider,
    )
    result = await service.send_template(
        Session(), SimpleNamespace(id=uuid4()), "sms-error-001", sms_request()
    )
    assert result.status == status
    assert billing.releases == releases
    assert billing.charges == 0


@pytest.mark.asyncio
async def test_sms_idempotency_replay_and_changed_parameters() -> None:
    provider, repository, billing = AdvantaProvider(), Repository(), Billing()
    service = MessagingService(
        Provider(),
        repository,
        TemplateRepository(sms_template()),
        billing,
        advanta_client=provider,
    )
    application = SimpleNamespace(id=uuid4())
    original = sms_request()
    first = await service.send_template(Session(), application, "sms-replay-001", original)
    repository.existing = first
    assert await service.send_template(Session(), application, "sms-replay-001", original) is first
    assert len(provider.calls) == 1
    with pytest.raises(IdempotencyConflictError):
        await service.send_template(
            Session(),
            application,
            "sms-replay-001",
            sms_request(parameters={"content": "Changed"}),
        )
