from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_authenticated_application
from app.db.session import get_db_session


class ScalarResult:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class Totals:
    def one(self):
        return (1, 125)


class Session:
    def __init__(self, account, usage=None):
        self.account = account
        self.usage = usage or []

    async def scalar(self, statement):
        return self.account

    async def scalars(self, statement):
        return ScalarResult(self.usage)

    async def execute(self, statement):
        return Totals()


async def call(app, path, session):
    async def session_override():
        yield session

    app.dependency_overrides[get_authenticated_application] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_db_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_balance_endpoint_returns_explicit_minor_unit_contract(app) -> None:
    account = SimpleNamespace(
        id=uuid4(),
        external_id="school-001",
        currency="KES",
        balance_minor=487_500,
        reserved_minor=12_500,
    )
    response = await call(app, "/api/v1/billing-accounts/school-001/balance", Session(account))
    assert response.status_code == 200
    assert response.json() == {
        "billing_account": "school-001",
        "currency": "KES",
        "balance_minor": 487_500,
        "balance": "4875.00",
        "reserved_minor": 12_500,
        "available_minor": 475_000,
    }


@pytest.mark.asyncio
async def test_unknown_or_cross_application_account_is_not_exposed(app) -> None:
    response = await call(app, "/api/v1/billing-accounts/other-app-account/balance", Session(None))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_usage_endpoint_is_bounded_and_aggregated(app) -> None:
    account_id = uuid4()
    usage = SimpleNamespace(
        id=uuid4(),
        outbound_message_id=uuid4(),
        channel="whatsapp",
        message_kind="template",
        billing_category="utility",
        currency="KES",
        customer_price_minor=125,
        created_at=datetime.now(UTC),
    )
    account = SimpleNamespace(id=account_id, external_id="school-001", currency="KES")
    response = await call(
        app,
        "/api/v1/billing-accounts/school-001/usage?limit=1&billing_category=utility",
        Session(account, [usage]),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 1
    assert body["total_customer_charge_minor"] == 125
    assert len(body["items"]) == 1
    invalid = await call(
        app, "/api/v1/billing-accounts/school-001/usage?limit=101", Session(account)
    )
    assert invalid.status_code == 422
