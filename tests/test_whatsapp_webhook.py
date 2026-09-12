import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.whatsapp_message import WhatsAppInboundMessage
from app.services.whatsapp_webhook import extract_inbound_messages, verify_signature


def make_payload(*messages, include_messages=True):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": "123456789"},
    }
    if include_messages:
        value["messages"] = list(messages)
    else:
        value["statuses"] = [{"id": "wamid.status", "status": "delivered"}]
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "business-id", "changes": [{"field": "messages", "value": value}]}],
    }


def text_message(message_id="wamid.one", body="Hello"):
    return {
        "from": "254700000000",
        "id": message_id,
        "timestamp": "1720000000",
        "type": "text",
        "text": {"body": body},
    }


def signed_body(payload, secret="test-app-secret"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, f"sha256={digest}"


async def request(app, method, path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_valid_webhook_verification(app) -> None:
    response = await request(
        app,
        "GET",
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge-value",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-value"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "value"},
        {
            "hub.mode": "unsubscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "value",
        },
        {},
    ],
)
async def test_failed_webhook_verification(app, params) -> None:
    response = await request(app, "GET", "/webhooks/whatsapp", params=params)
    assert response.status_code == 403
    assert "test-verify-token" not in response.text


def test_signature_validation_unit_cases() -> None:
    from pydantic import SecretStr

    body = b'{"valid":true}'
    secret = SecretStr("secret")
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert verify_signature(body, f"sha256={digest}", secret)
    assert not verify_signature(body, None, secret)
    assert not verify_signature(body, "invalid", secret)
    assert not verify_signature(body, "sha256=not-hex", secret)
    assert not verify_signature(body, f"sha256={'0' * 64}", secret)
    assert not verify_signature(body + b" ", f"sha256={digest}", secret)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signature",
    [None, "invalid", "sha256=not-hex", f"sha256={'0' * 64}"],
)
async def test_post_rejects_invalid_signatures(app, signature) -> None:
    headers = {"X-Hub-Signature-256": signature} if signature else {}
    response = await request(
        app, "POST", "/webhooks/whatsapp", content=b'{"not":"trusted"}', headers=headers
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_post_rejects_body_changed_after_signing(app) -> None:
    body, signature = signed_body({"object": "whatsapp_business_account"})
    response = await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=body + b" ",
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_inbound_text_message_is_stored(app, message_repository) -> None:
    payload = make_payload(text_message())
    body, signature = signed_body(payload)
    response = await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    stored = message_repository.messages["wamid.one"]
    assert stored.sender_wa_id == "254700000000"
    assert stored.recipient_phone_number_id == "123456789"
    assert stored.message_type == "text"
    assert stored.text_body == "Hello"
    assert stored.raw_payload["message"]["id"] == "wamid.one"


@pytest.mark.asyncio
async def test_multiple_messages_are_stored(app, message_repository) -> None:
    body, signature = signed_body(
        make_payload(text_message("wamid.one"), text_message("wamid.two", "Second"))
    )
    response = await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200
    assert set(message_repository.messages) == {"wamid.one", "wamid.two"}


@pytest.mark.asyncio
async def test_duplicate_then_new_message_preserves_new_message(app, message_repository) -> None:
    first_body, first_signature = signed_body(make_payload(text_message("wamid.one")))
    retry_body, retry_signature = signed_body(
        make_payload(text_message("wamid.one"), text_message("wamid.two"))
    )
    await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=first_body,
        headers={"X-Hub-Signature-256": first_signature},
    )
    await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=retry_body,
        headers={"X-Hub-Signature-256": retry_signature},
    )
    assert set(message_repository.messages) == {"wamid.one", "wamid.two"}


@pytest.mark.asyncio
async def test_status_only_and_irrelevant_payloads_are_acknowledged(
    app, message_repository
) -> None:
    for payload in [make_payload(include_messages=False), {"object": "page", "entry": []}]:
        body, signature = signed_body(payload)
        response = await request(
            app,
            "POST",
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature},
        )
        assert response.status_code == 200
    assert message_repository.messages == {}


@pytest.mark.asyncio
async def test_unsupported_message_type_is_stored_without_invented_content(
    app, message_repository
) -> None:
    image = text_message("wamid.image") | {"type": "image", "image": {"id": "media-id"}}
    image.pop("text")
    body, signature = signed_body(make_payload(image))
    response = await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200
    stored = message_repository.messages["wamid.image"]
    assert stored.message_type == "image"
    assert stored.text_body is None


@pytest.mark.asyncio
async def test_malformed_valid_payload_is_ignored(app, message_repository) -> None:
    malformed = make_payload({"type": "text", "timestamp": "not-a-time"})
    body, signature = signed_body(malformed)
    response = await request(
        app,
        "POST",
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200
    assert message_repository.messages == {}


def test_model_has_database_unique_constraint() -> None:
    constraints = WhatsAppInboundMessage.__table__.constraints
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("meta_message_id",) in unique_columns


def test_parser_handles_one_text_and_unsupported_message() -> None:
    image = text_message("wamid.image") | {"type": "image"}
    image.pop("text")
    parsed = extract_inbound_messages(make_payload(text_message(), image))
    assert [message.meta_message_id for message in parsed] == ["wamid.one", "wamid.image"]
    assert parsed[0].text_body == "Hello"
    assert parsed[1].text_body is None
