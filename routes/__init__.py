"""Routes for OC Table Editor."""
from fastapi import APIRouter
from .upload import router as upload_router
from .edit import router as edit_router
from .export import router as export_router
from .draft import router as draft_router

# Create main router
router = APIRouter()

# Include sub-routers
router.include_router(upload_router, prefix="/upload", tags=["upload"])
router.include_router(edit_router, prefix="/edit", tags=["edit"])
router.include_router(export_router, prefix="/export", tags=["export"])
router.include_router(draft_router, prefix="/draft", tags=["draft"])

__all__ = ['router']