from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedApplication
from app.db.session import get_db_session
from app.repositories.message_templates import MessageTemplateRepository
from app.schemas.messages import TemplateCapability, TemplateCapabilityList
from app.services.template_parameters import validate_parameter_schema

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


@router.get("", response_model=TemplateCapabilityList, summary="List available templates")
async def list_templates(
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TemplateCapabilityList:
    templates = await MessageTemplateRepository().list_active(session, application.id)
    return TemplateCapabilityList(
        items=[
            TemplateCapability(
                key=template.template_key,
                channel=template.channel,
                language=template.language_code,
                parameters=validate_parameter_schema(template.parameter_schema)["body"],
            )
            for template in templates
        ]
    )
