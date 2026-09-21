import httpx
import pytest
from pydantic import SecretStr

from app.services.advanta_client import AdvantaAPIError, AdvantaClient


def client(handler) -> AdvantaClient:
    return AdvantaClient(
        base_url="https://advanta.invalid",
        api_key=SecretStr("test-api-key"),
        partner_id=SecretStr("test-partner"),
        sender_id="FAZILABS",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "endpoint"), [("standard", "sendsms"), ("transactional", "sendotp")]
)
async def test_send_routes_and_sender_are_server_controlled(route: str, endpoint: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.url.path.endswith(endpoint)
        assert body["shortcode"] == "FAZILABS"
        return httpx.Response(
            200,
            json={
                "responses": [
                    {
                        "response-code": 200,
                        "response-description": "Success",
                        "mobile": "redacted-in-test",
                        "messageid": "adv-123",
                        "networkid": 1,
                    }
                ]
            },
        )

    result = await client(handler).send(mobile="254712345678", message="Safe text", route=route)
    assert result.provider_message_id == "adv-123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "ambiguous"),
    [
        ({"response-code": 1003}, False),
        ({"response-code": 4090}, True),
        ({"response-code": 200}, True),
    ],
)
async def test_provider_result_classification(body: dict, ambiguous: bool) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(AdvantaAPIError) as raised:
        await client(handler).send(mobile="254712345678", message="Safe", route="standard")
    assert raised.value.ambiguous_delivery is ambiguous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "ambiguous"),
    [(1003, False), (1006, False), (1005, True), (4090, True), (7777, True)],
)
async def test_nested_provider_result_classification(code: int, ambiguous: bool) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "responses": [
                    {
                        "response-code": code,
                        "response-description": "sensitive provider detail",
                        "mobile": "254700000000",
                    }
                ]
            },
        )

    with pytest.raises(AdvantaAPIError) as raised:
        await client(handler).send(
            mobile="254712345678", message="private message", route="standard"
        )
    assert raised.value.error_code == str(code)
    assert raised.value.ambiguous_delivery is ambiguous
    assert "sensitive" not in str(raised.value)
    assert "254" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"responses": []},
        {
            "responses": [
                {"response-code": 200, "messageid": "one"},
                {"response-code": 200, "messageid": "two"},
            ]
        },
        {"responses": ["malformed"]},
        {"responses": [{"response-code": 200}]},
        {"responses": [{"messageid": "missing-code"}]},
        {"responses": "not-a-list"},
    ],
)
async def test_malformed_nested_send_responses_are_uncertain(body: dict) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(AdvantaAPIError) as raised:
        await client(handler).send(mobile="254712345678", message="private", route="standard")
    assert raised.value.ambiguous_delivery


@pytest.mark.asyncio
async def test_http_5xx_is_ambiguous_and_errors_do_not_leak_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="secret body token")

    with pytest.raises(AdvantaAPIError) as raised:
        await client(handler).send(
            mobile="254712345678", message="secret body token", route="standard"
        )
    assert raised.value.ambiguous_delivery
    assert "secret body token" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "ambiguous"),
    [
        (httpx.ConnectError("connect failed"), False),
        (httpx.ReadTimeout("read timed out"), True),
        (httpx.ReadError("connection reset"), True),
    ],
)
async def test_network_outcome_classification(error: Exception, ambiguous: bool) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(AdvantaAPIError) as raised:
        await client(handler).send(mobile="254712345678", message="Safe", route="standard")
    assert raised.value.ambiguous_delivery is ambiguous


@pytest.mark.asyncio
async def test_dlr_and_balance_capabilities() -> None:
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("getbalance"):
            return httpx.Response(
                200,
                json={"response-code": 200, "credit": "800.00", "partner-id": "private"},
            )
        return httpx.Response(200, json={"delivery-description": "SentToNetwork"})

    advanta = client(handler)
    assert await advanta.get_delivery_report("adv-1") == {"delivery-description": "SentToNetwork"}
    balance = await advanta.get_balance()
    assert balance.display_credit == "800.00"
    assert paths[0].endswith("getdlr")
    assert paths[1].endswith("getbalance")


@pytest.mark.asyncio
async def test_balance_provider_error_is_not_converted_to_empty_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response-code": 1006, "response-description": "Invalid credentials"},
        )

    with pytest.raises(AdvantaAPIError, match=r"invalid credentials.*1006") as raised:
        await client(handler).get_balance()
    assert raised.value.error_code == "1006"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"response-code": 200},
        {"response-code": 200, "credit": "not-a-number"},
        {"response-code": 200, "balance": "800.00"},
    ],
)
async def test_unknown_balance_shapes_fail_actionably(body: dict) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(AdvantaAPIError, match="Advanta balance response"):
        await client(handler).get_balance()
