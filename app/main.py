from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging

API_DESCRIPTION = """
Fazilabs Messaging is the centralized consumer API for sending and tracking messages.

Consumer applications authenticate with an application API key in
`Authorization: Bearer <application-api-key>`. Send operations also require a
caller-generated `Idempotency-Key`; retry the same logical operation with the same key and
identical request to avoid duplicate provider submissions and charges.

Template keys are semantic and application-scoped. Their provider mapping, billing mode,
price, sender identity, and credentials remain server-side. Provider-facing webhook routes
are separate from the authenticated consumer API.
"""

OPENAPI_TAGS = [
    {
        "name": "System",
        "description": (
            "Service metadata, process liveness at `/health`, and PostgreSQL-backed application "
            "readiness at `/ready`."
        ),
    },
    {
        "name": "Messages",
        "description": "Authenticated, idempotent message submission and status queries.",
    },
    {
        "name": "Templates",
        "description": "Application-visible semantic template capabilities by channel.",
    },
    {
        "name": "Billing",
        "description": "Read-only prepaid wallet balance and immutable usage views.",
    },
    {
        "name": "Webhooks",
        "description": "Provider-facing verification, event, and delivery-report receivers.",
    },
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    application = FastAPI(
        title=app_settings.app_name,
        summary="Centralized, provider-independent messaging for Fazilabs applications",
        description=API_DESCRIPTION,
        version="0.1.0",
        contact={"name": "Fazilabs"},
        openapi_tags=OPENAPI_TAGS,
        debug=app_settings.debug,
    )
    application.state.settings = app_settings
    application.include_router(api_router)
    return application


app = create_app()
