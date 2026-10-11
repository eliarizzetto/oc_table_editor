"""Export operations routes."""
import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from io import StringIO

from services import SessionManager, CSVExporter
from services.session_document import document_cache
from services.table_scope import resolve_table_type
from services.view_builder import load_journal_view

router = APIRouter()


class ExportRequest(BaseModel):
    """Request model for exporting data."""
    session_id: str
    table_type: Optional[str] = None   # 'meta' or 'cits'; None = primary table
    revalidate: bool = False


@router.post("/")
async def export_csv(request: ExportRequest):
    """
    Export current table data to CSV format.

    - **session_id**: Session identifier
    - **table_type**: Which table to export ('meta'/'cits'; in paired
      sessions each table is exported with its own journal view)
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

    # Determine the table to export (both tables of a paired session are
    # independently editable and exportable).
    table_type = resolve_table_type(session, request.table_type)

    # Rows come from the journal replay (baseline + events) — no HTML parsing.
    async with document_cache.session_lock(request.session_id):
        loaded = await load_journal_view(request.session_id, table_type)
        if loaded is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        _journal, view, _state = loaded
        rows_data = await asyncio.to_thread(view.rows_for_export)

    # Generate CSV from parsed data
    original_csv_path = (session.meta_csv_path if table_type == 'meta'
                         else session.cits_csv_path)
    csv_content = await asyncio.to_thread(CSVExporter.rows_to_csv, rows_data, original_csv_path)

    # Determine filename
    filename_prefix = "metadata" if table_type == 'meta' else "citations"

    # Return CSV as downloadable file
    return StreamingResponse(
        StringIO(csv_content),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename_prefix}_edited.csv"
        }
    )
