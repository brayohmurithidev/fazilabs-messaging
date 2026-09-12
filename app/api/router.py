from fastapi import APIRouter

from app.api.routes import billing, health, messages, root, templates, whatsapp

api_router = APIRouter()
api_router.include_router(root.router)
api_router.include_router(health.router)
api_router.include_router(whatsapp.router)
api_router.include_router(messages.router)
api_router.include_router(templates.router)
api_router.include_router(billing.router)
