"""Export operations routes."""
import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from io import StringIO

from services import SessionManager, HTMLParser, CSVExporter
from services.session_document import document_cache
from models import Session

router = APIRouter()


class ExportRequest(BaseModel):
    """Request model for exporting data."""
    session_id: str
    revalidate: bool = False


@router.post("/")
async def export_csv(request: ExportRequest):
    """
    Export current HTML data to CSV format.
    
    - **session_id**: Session identifier
    - **revalidate**: Whether to re-validate before exporting (only if edits made)
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Check if revalidation is needed
    if request.revalidate:
        if not session.has_edits_since_validation:
            return {
                "warning": "No edits made since last validation. Re-validation skipped.",
                "exported": False
            }
    
    # Determine table type for HTML loading
    table_type = 'meta' if session.has_metadata else 'cits'

    # Load current HTML via the document cache and parse rows from the
    # (lazily built, shared) tree instead of re-parsing the whole document.
    async with document_cache.session_lock(request.session_id):
        doc = await document_cache.get_document(request.session_id, table_type)
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        soup = await doc.ensure_soup()
        rows_data = await asyncio.to_thread(HTMLParser.parse_table_from_soup, soup)

    # Generate CSV from parsed data
    original_csv_path = session.meta_csv_path if session.has_metadata else session.cits_csv_path
    csv_content = await asyncio.to_thread(CSVExporter.rows_to_csv, rows_data, original_csv_path)
    
    # Determine filename
    if session.has_metadata:
        filename_prefix = "metadata"
    else:
        filename_prefix = "citations"
    
    # Return CSV as downloadable file
    return StreamingResponse(
        StringIO(csv_content),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename_prefix}_edited.csv"
        }
    )
