"""Main routes."""

from fastapi import APIRouter

from media_service.app.routes import (
    admin,
    category,
    dashboard,
    internal,
    objects,
    presets,
    shares,
    transfer,
    uploads,
    variants,
)

api_router = APIRouter()
api_router.include_router(dashboard.router)
api_router.include_router(category.router)
api_router.include_router(uploads.router, prefix="/v1")
# The objects/variants/shares surfaces are each split across two routers by
# authorization tier (A16): the mutation router carries a mounted floor, the
# read router carries none because it admits anonymous callers on PUBLIC
# records. Registering both under the same prefix is the pattern this service
# already used for objects + variants.
api_router.include_router(objects.read_router, prefix="/v1")
api_router.include_router(objects.router, prefix="/v1")
api_router.include_router(shares.public_router, prefix="/v1")
api_router.include_router(shares.router, prefix="/v1")
api_router.include_router(variants.read_router, prefix="/v1")
api_router.include_router(variants.router, prefix="/v1")
api_router.include_router(presets.router, prefix="/v1")
api_router.include_router(transfer.router, prefix="/v1")
api_router.include_router(internal.router, prefix="/v1")
api_router.include_router(admin.router, prefix="/v1")
