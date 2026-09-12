from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_keys import api_key_prefix, verify_api_key
from app.db.session import get_db_session
from app.models.messaging_application import MessagingApiKey, MessagingApplication

bearer = HTTPBearer(auto_error=False, scheme_name="Messaging application API key")


async def get_authenticated_application(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MessagingApplication:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    prefix = api_key_prefix(credentials.credentials)
    if prefix is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    row = (
        await session.execute(
            select(MessagingApiKey, MessagingApplication)
            .join(MessagingApplication, MessagingApplication.id == MessagingApiKey.application_id)
            .where(MessagingApiKey.key_prefix == prefix)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    api_key, application = row
    if api_key.status != "active" or not verify_api_key(credentials.credentials, api_key.key_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if application.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Application disabled")
    try:
        await session.execute(
            update(MessagingApiKey)
            .where(MessagingApiKey.id == api_key.id)
            .values(last_used_at=datetime.now(UTC))
        )
        await session.commit()
    except Exception:
        await session.rollback()
        request.app.state.auth_usage_update_failures = (
            getattr(request.app.state, "auth_usage_update_failures", 0) + 1
        )
    return application


AuthenticatedApplication = Annotated[MessagingApplication, Depends(get_authenticated_application)]
