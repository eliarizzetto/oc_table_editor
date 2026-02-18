"""Edit operations routes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Optional
import sys
from pathlib import Path

from services import SessionManager, HTMLParser, ValidatorService, CSVExporter
from models import Session, EditState
from config import TEMP_DIR

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

# Import oc_validator interface for HTML generation and merging
from oc_validator.interface.gui import make_gui, merge_html_files

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_html(csv_fp: str, report_fp: str, out_fp: str, errors: list) -> None:
    """
    Generate an HTML visualisation for a validated CSV table.

    Safely handles the zero-errors case: when the validation report is empty
    ``make_gui`` crashes (it tries to open ``'valid_page.html'`` via a bare
    relative path that does not exist in this project).  We detect this and
    delegate to ``ValidatorService._make_no_errors_html`` instead.
    """
    if not errors:
        ValidatorService._make_no_errors_html(out_fp, csv_fp)
    else:
        make_gui(csv_fp, report_fp, out_fp)


def _table_type_for_display(session: Session) -> str:
    """
    Return the table_type key to pass to ``SessionManager.load_html`` when
    loading HTML *for display* in the editor.

    - Paired sessions → ``'display'``  (the merged view)
    - Single metadata → ``'meta'``
    - Single citations → ``'cits'``
    """
    if session.has_metadata and session.has_citations:
        return 'display'
    elif session.has_metadata:
        return 'meta'
    else:
        return 'cits'


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EditItemRequest(BaseModel):
    session_id: str
    item_id: str
    new_value: str


class AddItemRequest(BaseModel):
    session_id: str
    item_id: str   # ID of any existing .item-container in the target cell


class RevalidateRequest(BaseModel):
    session_id: str
    verify_id_existence: Optional[bool] = None


class GetFilteredRowsRequest(BaseModel):
    session_id: str
    issue_id: str


class DeleteRowRequest(BaseModel):
    session_id: str
    row_id: str   # e.g. "row5"


class ClearCellRequest(BaseModel):
    session_id: str
    row_id: str       # e.g. "row5"
    field_name: str   # e.g. "id", "author"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/html/{session_id}")
async def get_html(session_id: str):
    """
    Get current HTML content for a session.

    For paired sessions (metadata + citations) the merged display file is
    returned.  For single-table sessions the individual table HTML is returned.
    """
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _table_type_for_display(session)
    if table_type is None:
        raise HTTPException(status_code=400, detail="No data files found in session")

    html_content = await SessionManager.load_html(session_id, table_type)

    if not html_content:
        session_dir = TEMP_DIR / session_id
        if not session_dir.exists():
            raise HTTPException(status_code=404,
                                detail=f"Session directory not found: {session_dir}")
        existing_files = list(session_dir.glob('*.html'))
        if not existing_files:
            raise HTTPException(
                status_code=404,
                detail=f"No HTML files found in session directory."
            )
        raise HTTPException(
            status_code=404,
            detail=f"HTML content not found for table_type='{table_type}'. "
                   f"Found HTML files: {[f.name for f in existing_files]}"
        )

    # Apply edit-tracking highlights (grey background on edited items).
    # For paired sessions this is applied to the merged display HTML; item IDs
    # that correspond to meta-table edits are present in the merged HTML, so
    # apply_edit_tracking works correctly regardless.
    edit_states = await SessionManager.load_edit_state(session_id)
    if edit_states:
        edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
        html_content = HTMLParser.apply_edit_tracking(html_content, edited_ids)

    return {"html": html_content}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item in the table.

    The edit is always applied to the *individual* table HTML file
    (``meta_table.html`` for metadata, ``cits_table.html`` for citations).
    For paired sessions the merged display file (``meta_html.html``) is NOT
    updated immediately; it will be regenerated the next time the user
    triggers re-validation.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Always edit the individual (parseable) HTML, never the merged display.
    table_type = 'meta' if session.has_metadata else 'cits'

    html_content = await SessionManager.load_html(request.session_id, table_type)
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")

    original_value = HTMLParser.get_field_data_by_item_id(html_content, request.item_id)
    if original_value is None:
        raise HTTPException(status_code=404,
                            detail=f"Item '{request.item_id}' not found")

    updated_html = HTMLParser.update_item_value(html_content, request.item_id, request.new_value)

    # Auto-remove empty items from multi-value fields so that no stray
    # separators are left in the HTML (and therefore in the exported CSV).
    _MULTI_VALUE_FIELDS = set(HTMLParser.ITEM_SEPARATORS.keys())
    parts = request.item_id.split('-')
    if len(parts) >= 3:
        # item_id format: "{row}-{field}-{index}"
        # field name is everything between the first and last component
        field_name = '-'.join(parts[1:-1])
        if field_name in _MULTI_VALUE_FIELDS and request.new_value.strip() == '':
            updated_html = HTMLParser.remove_item(updated_html, request.item_id)

    await SessionManager.save_html(request.session_id, updated_html, table_type)

    # Track the edit
    edit_states = await SessionManager.load_edit_state(request.session_id)
    if request.item_id not in edit_states:
        edit_states[request.item_id] = EditState(
            item_id=request.item_id,
            original_value=original_value,
            edited_value=request.new_value,
            edited=True
        )
    else:
        edit_states[request.item_id].edited_value = request.new_value
        edit_states[request.item_id].edited = True

    await SessionManager.save_edit_state(request.session_id, edit_states)

    session.mark_edited()
    await SessionManager.save_session(session)

    return {
        "success": True,
        "original_value": original_value,
        "new_value": request.new_value
    }


@router.post("/item/add")
async def add_item_to_cell(request: AddItemRequest):
    """
    Append a new empty item slot to a multi-value cell.

    Inserts a new empty .item-container at the end of the cell that contains
    the referenced item_id, and adds the appropriate .sep span to the
    previously-last container.  The caller should reload the table after
    this call and click the new empty slot to fill it in.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Validate that the field is a multi-value field
    parts = request.item_id.split('-')
    if len(parts) < 3:
        raise HTTPException(status_code=400,
                            detail=f"Invalid item_id format: '{request.item_id}'")
    field_name = '-'.join(parts[1:-1])
    if field_name not in HTMLParser.ITEM_SEPARATORS:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{field_name}' is not a multi-value field"
        )

    separator = HTMLParser.ITEM_SEPARATORS[field_name]

    # Always operate on the individual (parseable) HTML
    table_type = 'meta' if session.has_metadata else 'cits'

    html_content = await SessionManager.load_html(request.session_id, table_type)
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")

    new_html, new_item_id = HTMLParser.add_item(html_content, request.item_id, separator)
    if not new_item_id:
        raise HTTPException(status_code=404,
                            detail=f"Item '{request.item_id}' not found in HTML")

    await SessionManager.save_html(request.session_id, new_html, table_type)

    session.mark_edited()
    await SessionManager.save_session(session)

    return {
        "success": True,
        "new_item_id": new_item_id
    }


@router.post("/row/delete")
async def delete_row(request: DeleteRowRequest):
    """
    Delete an entire table row from the individual HTML file.

    The row is identified by its ``<tr id="rowN">`` attribute.  After deletion
    the user should re-validate to export the updated table without this row.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = 'meta' if session.has_metadata else 'cits'
    html_content = await SessionManager.load_html(request.session_id, table_type)
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")

    updated_html = HTMLParser.delete_row(html_content, request.row_id)
    await SessionManager.save_html(request.session_id, updated_html, table_type)

    session.mark_edited()
    await SessionManager.save_session(session)

    return {"success": True, "row_id": request.row_id}


@router.post("/cell/clear")
async def clear_cell_route(request: ClearCellRequest):
    """
    Clear all values from a single cell, leaving one empty item-container.

    Works for both multi-value and single-value fields.  Also serves as the
    "initialise" endpoint for cells that currently have no item-containers at
    all (e.g. a field that was empty in the original CSV).
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = 'meta' if session.has_metadata else 'cits'
    html_content = await SessionManager.load_html(request.session_id, table_type)
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")

    new_html, new_item_id = HTMLParser.clear_cell(
        html_content, request.row_id, request.field_name
    )
    if not new_item_id:
        raise HTTPException(
            status_code=404,
            detail=f"Cell '{request.field_name}' not found in row '{request.row_id}'"
        )

    await SessionManager.save_html(request.session_id, new_html, table_type)

    session.mark_edited()
    await SessionManager.save_session(session)

    return {"success": True, "new_item_id": new_item_id}


@router.post("/revalidate")
async def revalidate(request: RevalidateRequest):
    """
    Re-run validation on the current (possibly edited) table data and regenerate
    the HTML view so that issue squares and the error-count headline reflect the
    latest validation results.

    For single-table sessions:
      1. Load the individual HTML (``meta_table.html`` or ``cits_table.html``).
      2. Parse it back to rows and export a temporary CSV.
      3. Run ``ValidatorService.validate_single`` on the temp CSV.
      4. Use the *returned* report path (not a post-hoc search) to call
         ``_generate_html``, which produces the new HTML.
      5. Save the new HTML back to the individual file.

    For paired sessions (metadata + citations):
      1. Load both individual HTMLs.
      2. Parse and export each to a separate temp CSV.
      3. Run ``ValidatorService.validate_pair`` (ClosureValidator).
      4. Regenerate both individual HTMLs from their respective new reports.
      5. Merge the two individual HTMLs and save the result as the display file
         (``meta_html.html``).
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    verify_id = (request.verify_id_existence
                 if request.verify_id_existence is not None
                 else session.verify_id_existence)

    session_dir = TEMP_DIR / request.session_id

    try:
        if session.has_metadata and session.has_citations:
            # ── Paired re-validation ────────────────────────────────────────
            meta_html = await SessionManager.load_html(request.session_id, 'meta')
            cits_html = await SessionManager.load_html(request.session_id, 'cits')

            if not meta_html:
                raise HTTPException(status_code=404,
                                    detail="Individual metadata HTML not found")
            if not cits_html:
                raise HTTPException(status_code=404,
                                    detail="Individual citations HTML not found")

            # Parse current table data from individual HTML files
            try:
                meta_rows = HTMLParser.parse_table(meta_html)
                cits_rows = HTMLParser.parse_table(cits_html)
            except Exception as e:
                raise ValueError(f"Failed to parse HTML tables: {e}")

            if not meta_rows:
                raise ValueError("No data found in metadata HTML table")
            if not cits_rows:
                raise ValueError("No data found in citations HTML table")

            # Export edited rows back to temporary CSV files
            meta_csv_content = CSVExporter.rows_to_csv(meta_rows, session.meta_csv_path)
            cits_csv_content = CSVExporter.rows_to_csv(cits_rows, session.cits_csv_path)

            temp_meta_csv = session_dir / 'temp_meta_revalidate.csv'
            temp_cits_csv = session_dir / 'temp_cits_revalidate.csv'

            with open(temp_meta_csv, 'w', encoding='utf-8') as f:
                f.write(meta_csv_content)
            with open(temp_cits_csv, 'w', encoding='utf-8') as f:
                f.write(cits_csv_content)

            # Run paired validation via ClosureValidator
            meta_errors, cits_errors, meta_report_path, cits_report_path = \
                ValidatorService.validate_pair(
                    meta_csv_path=str(temp_meta_csv),
                    cits_csv_path=str(temp_cits_csv),
                    meta_output_dir=str(session_dir),
                    cits_output_dir=str(session_dir),
                    verify_id_existence=verify_id
                )

            # Regenerate individual HTML files
            meta_table_path = session_dir / 'meta_table.html'
            cits_table_path = session_dir / 'cits_table.html'

            _generate_html(str(temp_meta_csv), meta_report_path,
                           str(meta_table_path), meta_errors)
            _generate_html(str(temp_cits_csv), cits_report_path,
                           str(cits_table_path), cits_errors)

            with open(meta_table_path, 'r', encoding='utf-8') as f:
                new_meta_html = f.read()
            with open(cits_table_path, 'r', encoding='utf-8') as f:
                new_cits_html = f.read()

            await SessionManager.save_html(request.session_id, new_meta_html, 'meta')
            await SessionManager.save_html(request.session_id, new_cits_html, 'cits')

            # Re-merge and save as display file
            merged_path = session_dir / 'meta_html.html'
            merge_html_files(str(meta_table_path), str(cits_table_path),
                             str(merged_path))
            with open(merged_path, 'r', encoding='utf-8') as f:
                merged_content = f.read()
            await SessionManager.save_html(request.session_id, merged_content, 'display')

            # Update session report paths
            session.meta_report_path = meta_report_path
            session.cits_report_path = cits_report_path

            total_error_count = len(meta_errors) + len(cits_errors)

            # Clean up temp files
            temp_meta_csv.unlink(missing_ok=True)
            temp_cits_csv.unlink(missing_ok=True)

        else:
            # ── Single-table re-validation ──────────────────────────────────
            table_type = 'meta' if session.has_metadata else 'cits'

            html_content = await SessionManager.load_html(request.session_id, table_type)
            if not html_content:
                raise HTTPException(status_code=404, detail="HTML content not found")

            # Parse current table data from HTML
            try:
                rows_data = HTMLParser.parse_table(html_content)
            except Exception as e:
                raise ValueError(f"Failed to parse HTML table: {e}")

            if not rows_data:
                raise ValueError("No data found in HTML table")

            # Export rows back to temporary CSV
            original_csv_path = (session.meta_csv_path if session.has_metadata
                                  else session.cits_csv_path)
            csv_content = CSVExporter.rows_to_csv(rows_data, original_csv_path)

            temp_csv_path = session_dir / 'temp_revalidate.csv'
            with open(temp_csv_path, 'w', encoding='utf-8') as f:
                f.write(csv_content)

            # Run validation — returns (errors, report_path) directly.
            # The report_path is taken from validator.output_fp_json, so it is
            # always the file that was *just* written, regardless of how many
            # previous runs have created incrementing suffixes in the directory.
            errors, report_path = ValidatorService.validate_single(
                csv_path=str(temp_csv_path),
                output_dir=str(session_dir),
                verify_id_existence=verify_id
            )

            # Generate new HTML using the freshly written report
            temp_html_path = session_dir / 'temp_revalidate.html'
            _generate_html(str(temp_csv_path), report_path,
                           str(temp_html_path), errors)

            with open(temp_html_path, 'r', encoding='utf-8') as f:
                new_html = f.read()

            # Save updated individual HTML (grey highlights intentionally
            # dropped — re-validation is the canonical "accept and re-check"
            # action; edited items are no longer specially marked afterwards).
            await SessionManager.save_html(request.session_id, new_html, table_type)

            # Update session report path
            if session.has_metadata:
                session.meta_report_path = report_path
            else:
                session.cits_report_path = report_path

            total_error_count = len(errors)

            # Clean up temp files
            temp_csv_path.unlink(missing_ok=True)
            temp_html_path.unlink(missing_ok=True)

        # Mark session as validated (clears has_edits_since_validation)
        session.mark_validated()
        session.verify_id_existence = verify_id
        await SessionManager.save_session(session)

        return {
            "success": True,
            "error_count": total_error_count,
            "html_updated": True
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Re-validation failed: {str(e)}")


@router.post("/filtered-rows")
async def get_filtered_rows(request: GetFilteredRowsRequest):
    """
    Get an HTML table fragment containing only the rows involved in a specific
    validation issue.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _table_type_for_display(session)
    html_content = await SessionManager.load_html(request.session_id, table_type)
    if not html_content:
        raise HTTPException(status_code=404, detail="HTML content not found")

    row_indices = HTMLParser.get_rows_by_issue(html_content, request.issue_id)
    filtered_html = HTMLParser.extract_filtered_table(html_content, row_indices)

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
    """Get list of edited items for a session."""
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    edit_states = await SessionManager.load_edit_state(session_id)

    edited_items = [
        {
            "item_id": item_id,
            "original_value": state.original_value,
            "edited_value": state.edited_value
        }
        for item_id, state in edit_states.items()
        if state.edited
    ]

    return {"edited_items": edited_items, "count": len(edited_items)}


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get session information."""
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
