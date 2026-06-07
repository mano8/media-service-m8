"""Main routes."""

from fastapi import APIRouter

from media_service.app.routes import admin, category, dashboard, objects, uploads

api_router = APIRouter()
api_router.include_router(dashboard.router)
api_router.include_router(category.router)
api_router.include_router(uploads.router, prefix="/v1")
api_router.include_router(objects.router, prefix="/v1")
api_router.include_router(admin.router, prefix="/v1")
