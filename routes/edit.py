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
    
    # Load HTML based on which files exist
    table_type = None
    if session.has_metadata:
        table_type = 'meta'
    elif session.has_citations:
        table_type = 'cits'
    
    if not table_type:
        raise HTTPException(status_code=400, detail="No data files found in session")
    
    html_content = await SessionManager.load_html(session_id, table_type)
    
    if not html_content:
        from config import TEMP_DIR
        session_dir = TEMP_DIR / session_id
        html_file = session_dir / f'{table_type}_html.html'
        
        # Check what files exist in the session directory
        if not session_dir.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"Session directory not found: {session_dir}"
            )
        
        existing_files = list(session_dir.glob('*.html'))
        if not existing_files:
            raise HTTPException(
                status_code=404,
                detail=f"No HTML files found in session directory. Expected: {html_file}"
            )
        
        raise HTTPException(
            status_code=404,
            detail=f"HTML content not found for table type '{table_type}'. Expected file: {html_file}. Found HTML files: {[f.name for f in existing_files]}"
        )
    
    # Apply edit tracking if exists
    edit_states = await SessionManager.load_edit_state(session_id)
    if edit_states:
        edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
        html_content = HTMLParser.apply_edit_tracking(html_content, edited_ids)
    
    return {"html": html_content}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item in table.
    
    - **session_id**: Session identifier
    - **item_id**: Unique identifier for item (e.g., '0-id-0')
    - **new_value**: New text value for item
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Determine table type
    table_type = 'meta' if session.has_metadata else 'cits'
    
    # Load current HTML
    html_content = await SessionManager.load_html(request.session_id, table_type)
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Get original value
    original_value = HTMLParser.get_field_data_by_item_id(html_content, request.item_id)
    
    if original_value is None:
        raise HTTPException(status_code=404, detail=f"Item '{request.item_id}' not found")
    
    # Update HTML with new value
    updated_html = HTMLParser.update_item_value(html_content, request.item_id, request.new_value)
    
    # Save updated HTML
    await SessionManager.save_html(request.session_id, updated_html, table_type)
    
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
    
    # Determine table type
    table_type = 'meta' if session.has_metadata else 'cits'
    
    # Load current HTML and parse it
    html_content = await SessionManager.load_html(request.session_id, table_type)
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Parse table data from HTML
    try:
        rows_data = HTMLParser.parse_table(html_content)
    except Exception as e:
        raise ValueError(f"Failed to parse HTML table: {str(e)}")
    
    if not rows_data:
        raise ValueError("No data found in HTML table")
    
    # Generate CSV from parsed data
    # Use correct HTML path to get session directory
    html_path = session.meta_html_path if table_type == 'meta' else session.cits_html_path
    if not html_path:
        raise HTTPException(
            status_code=500,
            detail=f"HTML path not found for table type '{table_type}'"
        )
    session_dir = Path(html_path).parent
    original_csv_path = session.meta_csv_path if session.has_metadata else session.cits_csv_path
    
    try:
        csv_content = CSVExporter.rows_to_csv(rows_data, original_csv_path)
    except Exception as e:
        raise ValueError(f"Failed to convert table to CSV: {str(e)}")
    
    # Save temporary CSV file
    temp_csv_path = session_dir / 'temp_revalidate.csv'
    with open(temp_csv_path, 'w', encoding='utf-8') as f:
        f.write(csv_content)
    
    try:
        # Run validation
        if session.has_metadata and session.has_citations:
            # Note: For paired files, we'd need both tables
            # For now, handle single table case
            errors = ValidatorService.validate_metadata(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        elif session.has_metadata:
            errors = ValidatorService.validate_metadata(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        else:
            errors = ValidatorService.validate_citations(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )
        
        # Get report path (use original CSV path, not temp CSV path)
        report_path = ValidatorService.get_report_json_path(original_csv_path, str(session_dir))
        
        if not report_path:
            raise ValueError("Validation report not found. Check that validation completed successfully.")
        
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
        await SessionManager.save_html(request.session_id, new_html, table_type)
        
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
            "error_count": len(errors),
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
    - **issue_id**: ID of issue (e.g., 'meta-0', 'cits-1')
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Determine table type
    table_type = 'meta' if session.has_metadata else 'cits'
    
    # Load current HTML
    html_content = await SessionManager.load_html(request.session_id, table_type)
    
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")
    
    # Get row indices for issue
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


@router.get("/edited/{session_id}")
async def get_edited_items(session_id: str):
    """
    Get list of edited items for a session.
    
    - **session_id**: Session identifier
    """
    session = await SessionManager.load_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    edit_states = await SessionManager.load_edit_state(session_id)
    
    # Filter only edited items
    edited_items = [
        {
            "item_id": item_id,
            "original_value": state.original_value,
            "edited_value": state.edited_value
        }
        for item_id, state in edit_states.items()
        if state.edited
    ]
    
    return {
        "edited_items": edited_items,
        "count": len(edited_items)
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