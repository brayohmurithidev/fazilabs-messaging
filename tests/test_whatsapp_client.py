import json

import httpx
import pytest
from pydantic import SecretStr

from app.services.template_parameters import UrlButtonValue
from app.services.whatsapp_client import (
    WhatsAppClientResponseError,
    WhatsAppCloudAPIClient,
    WhatsAppMalformedResponseError,
    WhatsAppNetworkError,
    WhatsAppServerResponseError,
    WhatsAppTimeoutError,
)


def client_with_handler(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WhatsAppCloudAPIClient(
        api_version="v99.0",
        phone_number_id="phone-id",
        access_token=SecretStr("super-secret-token"),
        http_client=http_client,
    )
    return client, http_client


@pytest.mark.asyncio
async def test_send_text_constructs_endpoint_headers_payload_and_context() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["content_type"] = request.headers["Content-Type"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.outbound"}]})

    client, http_client = client_with_handler(handler)
    try:
        result = await client.send_text_message(
            "254700000000", "Echo: hello", reply_to_message_id="wamid.inbound"
        )
    finally:
        await http_client.aclose()

    assert captured == {
        "url": "https://graph.facebook.com/v99.0/phone-id/messages",
        "authorization": "Bearer super-secret-token",
        "content_type": "application/json",
        "payload": {
            "messaging_product": "whatsapp",
            "to": "254700000000",
            "type": "text",
            "text": {"body": "Echo: hello"},
            "context": {"message_id": "wamid.inbound"},
        },
    }
    assert result.success is True
    assert result.recipient == "254700000000"
    assert result.meta_message_id == "wamid.outbound"


@pytest.mark.asyncio
async def test_send_template_builds_meta_body_components_in_order() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.template"}]})

    client, http_client = client_with_handler(handler)
    try:
        result = await client.send_template_message(
            "254700000000",
            provider_template_name="fazi_student_results_ready_v1",
            language_code="en_US",
            body_parameters=["Jane", "Brian", "Term 2"],
        )
    finally:
        await http_client.aclose()

    assert captured["payload"] == {
        "messaging_product": "whatsapp",
        "to": "254700000000",
        "type": "template",
        "template": {
            "name": "fazi_student_results_ready_v1",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Jane"},
                        {"type": "text", "text": "Brian"},
                        {"type": "text", "text": "Term 2"},
                    ],
                }
            ],
        },
    }
    assert result.meta_message_id == "wamid.template"


@pytest.mark.asyncio
async def test_send_template_builds_dynamic_url_button_components() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.dynamic"}]})

    client, http_client = client_with_handler(handler)
    try:
        await client.send_template_message(
            "254700000000",
            provider_template_name="provider_template",
            language_code="en_US",
            body_parameters=["Amina", "Baraka", "Term 2"],
            url_button_parameters=[
                UrlButtonValue(index=0, value="results/opaque-token"),
                UrlButtonValue(index=2, value="receipt/opaque-token"),
            ],
        )
    finally:
        await http_client.aclose()

    assert captured["payload"]["template"]["components"] == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Amina"},
                {"type": "text", "text": "Baraka"},
                {"type": "text", "text": "Term 2"},
            ],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "results/opaque-token"}],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "2",
            "parameters": [{"type": "text", "text": "receipt/opaque-token"}],
        },
    ]


@pytest.mark.asyncio
async def test_dynamic_url_value_is_redacted_from_provider_error_and_logs(caplog) -> None:
    credential = "results/secret-access-credential"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": f"invalid value {credential}"}})

    client, http_client = client_with_handler(handler)
    try:
        with pytest.raises(WhatsAppClientResponseError) as raised:
            await client.send_template_message(
                "254700000000",
                provider_template_name="provider_template",
                language_code="en_US",
                body_parameters=[],
                url_button_parameters=[UrlButtonValue(index=0, value=credential)],
            )
    finally:
        await http_client.aclose()

    assert credential not in str(raised.value)
    assert credential not in caplog.text
    assert "[redacted]" in str(raised.value)


@pytest.mark.asyncio
async def test_success_without_message_id_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "context" not in json.loads(request.content)
        return httpx.Response(200, json={"messaging_product": "whatsapp"})

    client, http_client = client_with_handler(handler)
    try:
        with pytest.raises(WhatsAppMalformedResponseError) as raised:
            await client.send_text_message("254700000000", "hello")
    finally:
        await http_client.aclose()
    assert raised.value.ambiguous_delivery is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "error_type", "ambiguous"),
    [
        (httpx.ConnectError("connection failed"), WhatsAppNetworkError, False),
        (httpx.ConnectTimeout("connect timed out"), WhatsAppNetworkError, False),
        (httpx.PoolTimeout("pool timed out"), WhatsAppNetworkError, False),
        (httpx.ReadTimeout("request timed out"), WhatsAppTimeoutError, True),
        (httpx.WriteTimeout("write timed out"), WhatsAppTimeoutError, True),
        (httpx.ReadError("connection reset"), WhatsAppNetworkError, True),
        (httpx.WriteError("write failed"), WhatsAppNetworkError, True),
        (httpx.RemoteProtocolError("invalid response"), WhatsAppNetworkError, True),
    ],
)
async def test_transport_errors_are_classified(exception, error_type, ambiguous) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        exception.request = request
        raise exception

    client, http_client = client_with_handler(handler)
    try:
        with pytest.raises(error_type) as raised:
            await client.send_text_message("254700000000", "hello")
    finally:
        await http_client.aclose()
    assert raised.value.ambiguous_delivery is ambiguous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(400, WhatsAppClientResponseError), (503, WhatsAppServerResponseError)],
)
async def test_meta_error_response_is_safely_parsed(status_code, error_type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "message": "Safe Meta diagnostic",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 123,
                }
            },
        )

    client, http_client = client_with_handler(handler)
    try:
        with pytest.raises(error_type) as raised:
            await client.send_text_message("254700000000", "hello")
    finally:
        await http_client.aclose()

    assert raised.value.status_code == status_code
    assert raised.value.error_code == "190"
    assert raised.value.error_subcode == "123"
    assert raised.value.error_type == "OAuthException"
    assert str(raised.value) == "Safe Meta diagnostic"
    assert raised.value.ambiguous_delivery is (status_code >= 500)


@pytest.mark.asyncio
async def test_malformed_success_response_is_translated() -> None:
    client, http_client = client_with_handler(
        lambda request: httpx.Response(200, content=b"not-json")
    )
    try:
        with pytest.raises(WhatsAppMalformedResponseError) as raised:
            await client.send_text_message("254700000000", "hello")
    finally:
        await http_client.aclose()
    assert raised.value.ambiguous_delivery is True


@pytest.mark.asyncio
async def test_access_token_is_removed_from_meta_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "token super-secret-token is invalid", "code": 190}},
        )

    client, http_client = client_with_handler(handler)
    try:
        with pytest.raises(WhatsAppClientResponseError) as raised:
            await client.send_text_message("254700000000", "hello")
    finally:
        await http_client.aclose()
    assert "super-secret-token" not in str(raised.value)
    assert "[redacted]" in str(raised.value)
