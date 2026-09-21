from fastapi import APIRouter, Request

router = APIRouter(tags=["System"])


@router.get(
    "/",
    summary="Get service metadata",
    description="Returns the configured service name and runtime environment.",
    response_description="Service identity and environment.",
)
async def service_metadata(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "name": settings.app_name,
        "environment": settings.environment.value,
    }
