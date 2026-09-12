from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.api.dependencies import get_authenticated_application
from app.core.api_keys import generate_api_key


class Result:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class AuthSession:
    def __init__(self, row):
        self.row = row
        self.committed = False

    async def execute(self, statement):
        return Result(self.row)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


def request() -> Request:
    return Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})


@pytest.mark.asyncio
async def test_missing_key_is_unauthorized() -> None:
    with pytest.raises(HTTPException) as raised:
        await get_authenticated_application(request(), None, AuthSession(None))
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_key_is_unauthorized() -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid")
    with pytest.raises(HTTPException) as raised:
        await get_authenticated_application(request(), credentials, AuthSession(None))
    assert raised.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_status", "app_status", "expected"),
    [("revoked", "active", 401), ("active", "disabled", 403)],
)
async def test_revoked_key_and_disabled_application(
    key_status: str, app_status: str, expected: int
) -> None:
    raw, prefix, encoded = generate_api_key()
    key = SimpleNamespace(id="key-id", key_prefix=prefix, key_hash=encoded, status=key_status)
    application = SimpleNamespace(status=app_status)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
    with pytest.raises(HTTPException) as raised:
        await get_authenticated_application(request(), credentials, AuthSession((key, application)))
    assert raised.value.status_code == expected


@pytest.mark.asyncio
async def test_valid_key_resolves_application_and_updates_usage() -> None:
    raw, prefix, encoded = generate_api_key()
    key = SimpleNamespace(id="key-id", key_prefix=prefix, key_hash=encoded, status="active")
    application = SimpleNamespace(status="active", slug="school-management")
    session = AuthSession((key, application))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
    resolved = await get_authenticated_application(request(), credentials, session)
    assert resolved is application
    assert session.committed
