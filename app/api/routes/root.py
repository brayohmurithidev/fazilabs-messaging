from fastapi import APIRouter, Request

router = APIRouter(tags=["service"])


@router.get("/")
async def service_metadata(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "name": settings.app_name,
        "environment": settings.environment.value,
    }
