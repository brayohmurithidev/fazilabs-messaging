from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.schemas.whatsapp import WhatsAppSendResult


@dataclass(frozen=True, slots=True)
class WhatsAppAPIError(Exception):
    message: str
    status_code: int | None = None
    error_code: str | None = None
    error_subcode: str | None = None
    error_type: str | None = None
    ambiguous_delivery: bool = False

    def __str__(self) -> str:
        return self.message


class WhatsAppNetworkError(WhatsAppAPIError):
    pass


class WhatsAppTimeoutError(WhatsAppAPIError):
    pass


class WhatsAppClientResponseError(WhatsAppAPIError):
    pass


class WhatsAppServerResponseError(WhatsAppAPIError):
    pass


class WhatsAppMalformedResponseError(WhatsAppAPIError):
    pass


class WhatsAppCloudAPIClient:
    supports_message_reconciliation_lookup = False

    def __init__(
        self,
        *,
        api_version: str,
        phone_number_id: str,
        access_token: SecretStr,
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._access_token = access_token
        self._http_client = http_client
        self._timeout = timeout or httpx.Timeout(5.0, connect=2.0)
        version = quote(api_version.strip(), safe="")
        phone_id = quote(phone_number_id.strip(), safe="")
        self._messages_url = f"https://graph.facebook.com/{version}/{phone_id}/messages"

    async def send_text_message(
        self,
        to: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> WhatsAppSendResult:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        if reply_to_message_id is not None:
            payload["context"] = {"message_id": reply_to_message_id}

        return await self._send(payload, recipient=to)

    async def send_template_message(
        self,
        to: str,
        *,
        provider_template_name: str,
        language_code: str,
        body_parameters: list[str],
    ) -> WhatsAppSendResult:
        components: list[dict[str, Any]] = []
        if body_parameters:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": parameter} for parameter in body_parameters
                    ],
                }
            )
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": provider_template_name,
                "language": {"code": language_code},
                "components": components,
            },
        }
        return await self._send(payload, recipient=to)

    async def _send(self, payload: dict[str, Any], *, recipient: str) -> WhatsAppSendResult:
        headers = {
            "Authorization": f"Bearer {self._access_token.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    self._messages_url, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._messages_url, headers=headers, json=payload)
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise WhatsAppNetworkError(
                "WhatsApp API connection could not be established", ambiguous_delivery=False
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            raise WhatsAppTimeoutError(
                "WhatsApp API request timed out", ambiguous_delivery=True
            ) from exc
        except httpx.ConnectError as exc:
            raise WhatsAppNetworkError(
                "WhatsApp API connection could not be established", ambiguous_delivery=False
            ) from exc
        except httpx.LocalProtocolError as exc:
            raise WhatsAppNetworkError(
                "WhatsApp API request was rejected locally", ambiguous_delivery=False
            ) from exc
        except httpx.RequestError as exc:
            raise WhatsAppNetworkError(
                "WhatsApp API network request outcome is unknown", ambiguous_delivery=True
            ) from exc

        if not response.is_success:
            self._raise_response_error(response)
        return self._parse_success(response, recipient=recipient)

    def _raise_response_error(self, response: httpx.Response) -> None:
        details = self._safe_error_details(response)
        error_class = (
            WhatsAppClientResponseError
            if 400 <= response.status_code < 500
            else WhatsAppServerResponseError
        )
        raise error_class(
            status_code=response.status_code,
            ambiguous_delivery=response.status_code >= 500,
            **details,
        )

    def _safe_error_details(self, response: httpx.Response) -> dict[str, str | None]:
        error: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                error = body["error"]
        except ValueError:
            pass

        message = error.get("message") if isinstance(error.get("message"), str) else None
        return {
            "message": self._sanitize(
                message or f"WhatsApp API returned HTTP {response.status_code}"
            ),
            "error_code": self._string_or_none(error.get("code")),
            "error_subcode": self._string_or_none(error.get("error_subcode")),
            "error_type": self._string_or_none(error.get("type")),
        }

    def _parse_success(self, response: httpx.Response, recipient: str) -> WhatsAppSendResult:
        try:
            body = response.json()
        except ValueError as exc:
            raise WhatsAppMalformedResponseError(
                "WhatsApp API returned a malformed success response", ambiguous_delivery=True
            ) from exc
        if not isinstance(body, dict):
            raise WhatsAppMalformedResponseError(
                "WhatsApp API returned an unexpected success response", ambiguous_delivery=True
            )

        meta_message_id = None
        messages = body.get("messages")
        if messages is not None:
            if not isinstance(messages, list) or (messages and not isinstance(messages[0], dict)):
                raise WhatsAppMalformedResponseError(
                    "WhatsApp API returned an unexpected messages field", ambiguous_delivery=True
                )
            if messages and isinstance(messages[0].get("id"), str):
                meta_message_id = messages[0]["id"]

        if not meta_message_id:
            raise WhatsAppMalformedResponseError(
                "WhatsApp API success response omitted message ID", ambiguous_delivery=True
            )

        return WhatsAppSendResult(
            recipient=recipient,
            meta_message_id=meta_message_id,
            success=True,
        )

    def _sanitize(self, message: str) -> str:
        token = self._access_token.get_secret_value()
        return message.replace(token, "[redacted]")[:1000]

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return str(value) if value is not None else None
