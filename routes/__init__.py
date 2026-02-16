"""Routes for OC Table Editor."""
from fastapi import APIRouter
from .upload import router as upload_router
from .edit import router as edit_router
from .export import router as export_router
from .draft import router as draft_router

# Create main router
router = APIRouter()

# Include sub-routers
router.include_router(upload_router, prefix="/api/upload", tags=["upload"])
router.include_router(edit_router, prefix="/api/edit", tags=["edit"])
router.include_router(export_router, prefix="/api/export", tags=["export"])
router.include_router(draft_router, prefix="/api/draft", tags=["draft"])

__all__ = ['router']