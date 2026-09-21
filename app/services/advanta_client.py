from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class AdvantaResult:
    provider_message_id: str


@dataclass(frozen=True, slots=True)
class AdvantaBalance:
    credit: Decimal

    @property
    def display_credit(self) -> str:
        return format(self.credit, "f")


@dataclass(frozen=True, slots=True)
class AdvantaAPIError(Exception):
    message: str
    error_code: str | None = None
    ambiguous_delivery: bool = False

    def __str__(self) -> str:
        return self.message


class AdvantaClient:
    _CONFIRMED_CODES = {
        "1001",
        "1002",
        "1003",
        "1004",
        "1006",
        "1008",
        "1009",
        "1010",
        "1012",
        "4091",
        "4092",
        "4093",
    }
    _AMBIGUOUS_CODES = {"1005", "1007", "4090"}
    _ERROR_DESCRIPTIONS = {
        "1006": "invalid credentials",
        "4091": "Partner ID is missing",
        "4092": "API key is missing",
        "4093": "account details were not found",
    }

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        partner_id: SecretStr,
        sender_id: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._partner_id = partner_id
        self._sender_id = sender_id
        self._http_client = http_client
        self._timeout = timeout or httpx.Timeout(5.0, connect=2.0)

    async def send(self, *, mobile: str, message: str, route: str) -> AdvantaResult:
        endpoint = "sendotp" if route == "transactional" else "sendsms"
        body = self._credentials() | {
            "message": message,
            "shortcode": self._sender_id,
            "mobile": mobile,
        }
        response = await self._post(endpoint, body, submission=True)
        return self._parse_send(response)

    async def get_delivery_report(self, provider_message_id: str) -> dict[str, Any]:
        response = await self._post(
            "getdlr", self._credentials() | {"messageid": provider_message_id}, submission=False
        )
        return self._json_object(response, "delivery report")

    async def get_balance(self) -> AdvantaBalance:
        response = await self._post("getbalance", self._credentials(), submission=False)
        return self._parse_balance(response)

    def _credentials(self) -> dict[str, str]:
        return {
            "apikey": self._api_key.get_secret_value(),
            "partnerID": self._partner_id.get_secret_value(),
        }

    async def _post(
        self, endpoint: str, body: dict[str, Any], *, submission: bool
    ) -> httpx.Response:
        url = f"{self._base_url}/api/services/{endpoint}"
        try:
            if self._http_client is not None:
                response = await self._http_client.post(url, json=body, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=body)
        except (
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.LocalProtocolError,
        ) as exc:
            raise AdvantaAPIError(
                "Advanta connection could not be established", ambiguous_delivery=False
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RequestError) as exc:
            raise AdvantaAPIError(
                "Advanta request outcome is unknown", ambiguous_delivery=submission
            ) from exc
        if not response.is_success:
            raise AdvantaAPIError(
                f"Advanta returned HTTP {response.status_code}",
                ambiguous_delivery=response.status_code >= 500,
            )
        return response

    def _parse_send(self, response: httpx.Response) -> AdvantaResult:
        body = self._json_object(response, "send")
        responses = body.get("responses")
        if responses is None:
            top_level_code = body.get("response-code", body.get("responseCode", body.get("code")))
            if top_level_code is not None and str(top_level_code) != "200":
                self._raise_send_error(str(top_level_code))
            raise AdvantaAPIError(
                "Advanta returned an unexpected send response", ambiguous_delivery=True
            )
        if not isinstance(responses, list) or len(responses) != 1:
            raise AdvantaAPIError(
                "Advanta returned an unexpected send response", ambiguous_delivery=True
            )
        item = responses[0]
        if not isinstance(item, dict):
            raise AdvantaAPIError(
                "Advanta returned an unexpected send response", ambiguous_delivery=True
            )
        code_value = item.get("response-code", item.get("responseCode", item.get("code")))
        if code_value is None:
            raise AdvantaAPIError(
                "Advanta send response omitted response code", ambiguous_delivery=True
            )
        code = str(code_value)
        if code != "200":
            self._raise_send_error(code)
        message_id = item.get("messageid") or item.get("messageId")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, (str, int))
            or not str(message_id).strip()
        ):
            raise AdvantaAPIError(
                "Advanta success response omitted message ID",
                error_code=code,
                ambiguous_delivery=True,
            )
        return AdvantaResult(str(message_id))

    def _raise_send_error(self, code: str) -> None:
        if code in self._CONFIRMED_CODES:
            raise AdvantaAPIError("Advanta rejected the SMS submission", error_code=code)
        raise AdvantaAPIError(
            "Advanta submission outcome is unknown", error_code=code, ambiguous_delivery=True
        )

    def _parse_balance(self, response: httpx.Response) -> AdvantaBalance:
        body = self._json_object(response, "balance")
        code_value = body.get("response-code", body.get("responseCode", body.get("code")))
        if code_value is None:
            raise AdvantaAPIError(
                "Advanta balance response omitted response code", ambiguous_delivery=False
            )
        code = str(code_value)
        if code != "200":
            reason = self._ERROR_DESCRIPTIONS.get(code, "provider rejected the balance request")
            raise AdvantaAPIError(
                f"Advanta balance request failed: {reason} (code {code})",
                error_code=code,
                ambiguous_delivery=False,
            )
        credit_value = body.get("credit")
        if isinstance(credit_value, bool) or not isinstance(credit_value, (str, int, float)):
            raise AdvantaAPIError(
                "Advanta balance response omitted credit", ambiguous_delivery=False
            )
        try:
            credit = Decimal(str(credit_value))
        except InvalidOperation as exc:
            raise AdvantaAPIError(
                "Advanta balance response contained invalid credit", ambiguous_delivery=False
            ) from exc
        if not credit.is_finite() or credit < 0:
            raise AdvantaAPIError(
                "Advanta balance response contained invalid credit", ambiguous_delivery=False
            )
        return AdvantaBalance(credit=credit)

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise AdvantaAPIError(
                f"Advanta returned a malformed {operation} response", ambiguous_delivery=True
            ) from exc
        if not isinstance(body, dict):
            raise AdvantaAPIError(
                f"Advanta returned an unexpected {operation} response", ambiguous_delivery=True
            )
        return body
