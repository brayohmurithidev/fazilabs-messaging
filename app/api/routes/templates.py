from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedApplication
from app.db.session import get_db_session
from app.repositories.message_templates import MessageTemplateRepository
from app.schemas.messages import TemplateCapability, TemplateCapabilityList
from app.services.template_parameters import semantic_parameter_names

router = APIRouter(prefix="/api/v1/templates", tags=["Templates"])


@router.get(
    "",
    response_model=TemplateCapabilityList,
    summary="List available templates",
    description=(
        "Lists active semantic template/channel mappings for the authenticated application. "
        "Parameter names can differ between channel mappings. `billing_mode` tells the "
        "consumer whether a customer wallet funds the template; provider details remain hidden."
    ),
    response_description="Active template capabilities visible to this application.",
    responses={401: {"description": "Missing or invalid application API key."}},
)
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
                parameters=semantic_parameter_names(template.parameter_schema),
                billing_mode=template.billing_mode,
                provider_route=getattr(template, "provider_route", None),
            )
            for template in templates
        ]
    )
