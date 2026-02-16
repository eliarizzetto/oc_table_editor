"""Export operations routes."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from io import StringIO
import sys
from pathlib import Path

from services import SessionManager, HTMLParser, CSVExporter
from models import Session

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

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
    
    # Load current HTML
    html_content = await SessionManager.load_html(request.session_id, 'meta')
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Parse table data from HTML
    rows_data = HTMLParser.parse_table(html_content)
    
    # Generate CSV from parsed data
    original_csv_path = session.meta_csv_path if session.has_metadata else session.cits_csv_path
    csv_content = CSVExporter.rows_to_csv(rows_data, original_csv_path)
    
    # Determine filename
    if session.has_metadata:
        table_type = "metadata"
    else:
        table_type = "citations"
    
    # Return CSV as downloadable file
    return StreamingResponse(
        StringIO(csv_content),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={table_type}_edited.csv"
        }
    )