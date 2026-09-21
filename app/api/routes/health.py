import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready"]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check process liveness",
    description=(
        "Returns successfully when the API process can serve requests. This is a liveness "
        "check and does not verify PostgreSQL or provider connectivity."
    ),
    response_description="The API process is alive.",
)
async def health() -> HealthResponse:
    """Report process liveness without accessing external dependencies."""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Check application readiness",
    description=(
        "Runs a minimal PostgreSQL connectivity check. Use this for readiness and traffic "
        "admission; use `/health` for process liveness. No provider is contacted."
    ),
    response_description="The application can reach PostgreSQL.",
    responses={
        503: {
            "description": "PostgreSQL is unavailable. The response contains no connection details."
        }
    },
)
async def ready(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReadinessResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("Database readiness check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "database_unavailable",
                "message": "Database is unavailable.",
            },
        ) from None
    return ReadinessResponse(status="ready")
