"""Edit operations routes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Optional
import sys
from pathlib import Path

from services import SessionManager, HTMLParser, ValidatorService, CSVExporter
from models import Session, EditState

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

# Import oc_validator interface for HTML generation
from oc_validator.interface.gui import make_gui

router = APIRouter()


class EditItemRequest(BaseModel):
    """Request model for editing a single item."""
    session_id: str
    item_id: str
    new_value: str


class RevalidateRequest(BaseModel):
    """Request model for re-validation."""
    session_id: str
    verify_id_existence: Optional[bool] = None


class GetFilteredRowsRequest(BaseModel):
    """Request model for getting rows by issue."""
    session_id: str
    issue_id: str


@router.get("/html/{session_id}")
async def get_html(session_id: str):
    """
    Get current HTML content for a session.
    
    - **session_id**: Session identifier
    """
    session = await SessionManager.load_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    html_content = await SessionManager.load_html(session_id, 'meta')
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Apply edit tracking if exists
    edit_states = await SessionManager.load_edit_state(session_id)
    if edit_states:
        edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
        html_content = HTMLParser.apply_edit_tracking(html_content, edited_ids)
    
    return {"html": html_content}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item in the table.
    
    - **session_id**: Session identifier
    - **item_id**: Unique identifier for the item (e.g., '0-id-0')
    - **new_value**: New text value for the item
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Load current HTML
    html_content = await SessionManager.load_html(request.session_id, 'meta')
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Get original value
    original_value = HTMLParser.get_field_data_by_item_id(html_content, request.item_id)
    
    if original_value is None:
        raise HTTPException(status_code=404, detail=f"Item '{request.item_id}' not found")
    
    # Update HTML with new value
    updated_html = HTMLParser.update_item_value(html_content, request.item_id, request.new_value)
    
    # Save updated HTML
    await SessionManager.save_html(request.session_id, updated_html, 'meta')
    
    # Update edit state
    edit_states = await SessionManager.load_edit_state(request.session_id)
    
    if request.item_id not in edit_states:
        # First edit for this item
        edit_states[request.item_id] = EditState(
            item_id=request.item_id,
            original_value=original_value,
            edited_value=request.new_value,
            edited=True
        )
    else:
        # Update existing edit state
        edit_states[request.item_id].edited_value = request.new_value
        edit_states[request.item_id].edited = True
    
    await SessionManager.save_edit_state(request.session_id, edit_states)
    
    # Mark session as edited
    session.mark_edited()
    await SessionManager.save_session(session)
    
    return {
        "success": True,
        "original_value": original_value,
        "new_value": request.new_value
    }


@router.post("/revalidate")
async def revalidate(request: RevalidateRequest):
    """
    Re-run validation on current HTML data.
    
    - **session_id**: Session identifier
    - **verify_id_existence**: Optional override for ID existence verification
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Use provided verify_id_existence or session default
    verify_id = request.verify_id_existence if request.verify_id_existence is not None else session.verify_id_existence
    
    # Load current HTML and parse it
    html_content = await SessionManager.load_html(request.session_id, 'meta')
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Parse table data from HTML
    rows_data = HTMLParser.parse_table(html_content)
    
    # Generate CSV from parsed data
    session_dir = Path(session.meta_html_path).parent
    original_csv_path = session.meta_csv_path if session.has_metadata else session.cits_csv_path
    
    csv_content = CSVExporter.rows_to_csv(rows_data, original_csv_path)
    
    # Save temporary CSV file
    temp_csv_path = session_dir / 'temp_revalidate.csv'
    with open(temp_csv_path, 'w', encoding='utf-8') as f:
        f.write(csv_content)
    
    try:
        # Run validation
        if session.has_metadata and session.has_citations:
            # Note: For paired files, we'd need both tables
            # For now, handle single table case
            report = ValidatorService.validate_metadata(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        elif session.has_metadata:
            report = ValidatorService.validate_metadata(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        else:
            report = ValidatorService.validate_citations(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        
        # Get report path
        report_path = ValidatorService.get_report_json_path(str(temp_csv_path), str(session_dir))
        
        # Generate new HTML
        temp_html_path = session_dir / 'temp_revalidate.html'
        make_gui(str(temp_csv_path), report_path, str(temp_html_path))
        
        # Load new HTML content
        with open(temp_html_path, 'r', encoding='utf-8') as f:
            new_html = f.read()
        
        # Re-apply edit tracking
        edit_states = await SessionManager.load_edit_state(request.session_id)
        if edit_states:
            edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
            new_html = HTMLParser.apply_edit_tracking(new_html, edited_ids)
        
        # Save updated HTML
        await SessionManager.save_html(request.session_id, new_html, 'meta')
        
        # Update session report path
        if session.has_metadata:
            session.meta_report_path = report_path
        else:
            session.cits_report_path = report_path
        
        # Mark as validated
        session.mark_validated()
        session.verify_id_existence = verify_id
        await SessionManager.save_session(session)
        
        # Clean up temp files
        temp_csv_path.unlink(missing_ok=True)
        temp_html_path.unlink(missing_ok=True)
        
        return {
            "success": True,
            "error_count": len(report),
            "html_updated": True
        }
        
    except Exception as e:
        # Clean up temp files
        temp_csv_path.unlink(missing_ok=True)
        temp_html_path.unlink(missing_ok=True)
        
        raise HTTPException(
            status_code=500,
            detail=f"Re-validation failed: {str(e)}"
        )


@router.post("/filtered-rows")
async def get_filtered_rows(request: GetFilteredRowsRequest):
    """
    Get HTML table containing only rows involved in a specific issue.
    
    - **session_id**: Session identifier
    - **issue_id**: ID of the issue (e.g., 'meta-0', 'cits-1')
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Load current HTML
    html_content = await SessionManager.load_html(request.session_id, 'meta')
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Get row indices for the issue
    row_indices = HTMLParser.get_rows_by_issue(html_content, request.issue_id)
    
    # Extract filtered table
    filtered_html = HTMLParser.extract_filtered_table(html_content, row_indices)
    
    # Apply edit tracking if exists
    edit_states = await SessionManager.load_edit_state(request.session_id)
    if edit_states:
        edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
        filtered_html = HTMLParser.apply_edit_tracking(filtered_html, edited_ids)
    
    return {
        "html": filtered_html,
        "row_indices": row_indices,
        "issue_id": request.issue_id
    }


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """
    Get session information.
    
    - **session_id**: Session identifier
    """
    session = await SessionManager.load_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    edit_states = await SessionManager.load_edit_state(session_id)
    edited_count = sum(1 for state in edit_states.values() if state.edited)
    
    return {
        "session_id": session.session_id,
        "has_metadata": session.has_metadata,
        "has_citations": session.has_citations,
        "verify_id_existence": session.verify_id_existence,
        "has_edits_since_validation": session.has_edits_since_validation,
        "edited_items_count": edited_count,
        "last_validated_at": session.last_validated_at
    }