"""Edit operations routes.

All mutations are row-scoped and run against the in-memory document cache
(``services.session_document``): the session's parsed tree is mutated once
and only the affected ``<tr>`` is re-serialised and spliced into the
canonical HTML string.  Responses additionally carry ``row_html`` /
``row_id`` so the frontend can patch a single row in place (Phase B) instead
of reloading the whole table.
"""
import asyncio

from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Optional
from bs4 import BeautifulSoup

from services import SessionManager, HTMLParser, ValidatorService, CSVExporter
from services.session_document import (
    SpliceError,
    SessionDocument,
    compose_display,
    document_cache,
    find_row_bounds,
    next_row_id_in_str,
)
from services.validator_service import load_jsonl_report
from models import Session, EditState, RowChangeState, DeletedItemState
from config import TEMP_DIR

# Import oc_validator interface for HTML generation and merging
from oc_validator.interface.gui import make_gui, merge_html_files

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_html(csv_fp: str, report_fp: str, out_fp: str, is_valid: bool) -> None:
    """
    Generate an HTML visualisation for a validated CSV table.

    Safely handles the zero-errors case: when the validation report is empty
    ``make_gui`` crashes (it tries to open ``valid_page.html`` via a bare
    relative path that does not exist in this project).  We detect this and
    delegate to ``ValidatorService._make_no_errors_html`` instead.
    """
    if is_valid:
        ValidatorService._make_no_errors_html(out_fp, csv_fp)
    else:
        make_gui(csv_fp, report_fp, out_fp)


def _editable_table_type(session: Session) -> str:
    """The table HTML that mutations operate on ('meta' or 'cits')."""
    return 'meta' if session.has_metadata else 'cits'


async def _get_editable_doc(session_id: str, session: Session) -> SessionDocument:
    """Load the editable document for a session or raise 404.

    Caller must hold the session lock.
    """
    table_type = _editable_table_type(session)
    doc = await document_cache.get_document(session_id, table_type)
    if doc is None:
        raise HTTPException(status_code=404, detail="HTML content not found")
    return doc


def _row_id_for_item(item_id: str) -> str:
    """'rowN' for an item id '{N}-{field}-{idx}'."""
    return f"row{item_id.split('-')[0]}"


def _mark_tracked_rows(html_content: str, edited_ids: List[str],
                       added_ids: List[str], added_row_ids: List[str]) -> str:
    """Apply edited/added tracking classes to the served HTML via row splices.

    Rows whose anchor cannot be found are skipped (mirrors the tolerant
    'if container:' behaviour of the old full-document tracking pass).
    """
    rows_to_mark = ({_row_id_for_item(i) for i in edited_ids}
                    | {_row_id_for_item(i) for i in added_ids}
                    | set(added_row_ids))
    for row_id in rows_to_mark:
        try:
            start, end = find_row_bounds(html_content, row_id)
        except SpliceError:
            continue
        marked = HTMLParser.apply_tracking_to_row_html(
            html_content[start:end], edited_ids, added_ids,
            row_id in added_row_ids
        )
        if marked != html_content[start:end]:
            html_content = html_content[:start] + marked + html_content[end:]
    return html_content


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EditItemRequest(BaseModel):
    session_id: str
    item_id: str
    new_value: str


class DeleteItemRequest(BaseModel):
    session_id: str
    item_id: str


class AddItemRequest(BaseModel):
    session_id: str
    item_id: Optional[str] = None   # ID of an existing item (for backward compatibility)
    row_id: Optional[str] = None    # Row ID for adding with value
    field_name: Optional[str] = None  # Field name for adding with value
    new_value: Optional[str] = None  # Value for the new item


class RevalidateRequest(BaseModel):
    session_id: str
    verify_id_existence: Optional[bool] = None


class GetFilteredRowsRequest(BaseModel):
    session_id: str
    issue_id: str


class DeleteRowRequest(BaseModel):
    session_id: str
    row_id: str   # e.g. "row5"


class AddRowRequest(BaseModel):
    session_id: str


class ClearCellRequest(BaseModel):
    session_id: str
    row_id: str       # e.g. "row5"
    field_name: str   # e.g. "id", "author"


class UndoRedoRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/html/{session_id}")
async def get_html(session_id: str):
    """
    Get current HTML content for a session.

    For paired sessions (metadata + citations) the display document is
    *derived* on the fly from the two canonical tables (the merged
    ``meta_html.html`` file is only written at upload/revalidate for
    compatibility), so edits and undo/redo are always reflected immediately.
    """
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.has_metadata and session.has_citations:
        meta_doc = await document_cache.get_document(session_id, 'meta')
        cits_doc = await document_cache.get_document(session_id, 'cits')
        if meta_doc is None or cits_doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        html_content = compose_display(meta_doc.canonical, cits_doc.canonical)
    elif session.has_metadata:
        doc = await document_cache.get_document(session_id, 'meta')
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        html_content = doc.canonical
    else:
        doc = await document_cache.get_document(session_id, 'cits')
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        html_content = doc.canonical

    # Apply edit-tracking highlights (grey background on edited items,
    # green on added items/rows) via row splices — no full re-parse.
    edit_states = await SessionManager.load_edit_state(session_id)
    row_change_states = await SessionManager.load_row_change_state(session_id)
    edited_ids = [i for i, s in edit_states.items() if s.edited]
    added_ids = [i for i, s in edit_states.items() if s.added]
    added_row_ids = [r for r, s in row_change_states.items() if s.added]
    if edited_ids or added_ids or added_row_ids:
        html_content = _mark_tracked_rows(
            html_content, edited_ids, added_ids, added_row_ids
        )

    return {"html": html_content}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item in the table.

    The edit is applied to the *individual* table HTML (``meta_table.html``
    for metadata, ``cits_table.html`` for citations).  For paired sessions
    the served display is derived from the individual tables, so the change
    is visible immediately.  The response carries the updated ``row_html``
    so the frontend can patch the row in place.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)
    row_id = _row_id_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        doc = await _get_editable_doc(request.session_id, session)
        try:
            soup = await doc.ensure_soup()

            original_value = HTMLParser.get_item_value_from_soup(soup, request.item_id)
            if original_value is None:
                raise HTTPException(status_code=404,
                                    detail=f"Item '{request.item_id}' not found")

            pre_row_html = doc.row_html_or_none(row_id)
            if pre_row_html is None:
                raise HTTPException(status_code=500,
                                    detail=f"Row '{row_id}' not found in document")

            # Snapshot for undo BEFORE applying the mutation
            await SessionManager.push_undo_row_snapshot(
                request.session_id, table_type, row_id, pre_row_html
            )

            HTMLParser.update_item_value_in_soup(soup, request.item_id, request.new_value)

            # Auto-remove empty items from multi-value fields so that no stray
            # separators are left in the HTML (and therefore in the exported CSV).
            _MULTI_VALUE_FIELDS = set(HTMLParser.ITEM_SEPARATORS.keys())
            parts = request.item_id.split('-')
            if len(parts) >= 3:
                field_name = '-'.join(parts[1:-1])
                if field_name in _MULTI_VALUE_FIELDS and request.new_value.strip() == '':
                    HTMLParser.remove_item_in_soup(soup, request.item_id)

            new_row_html = doc.commit_row(row_id)
            await doc.persist()
        except HTTPException:
            raise
        except Exception:
            # Tree and canonical string may have diverged — rebuild from disk
            document_cache.drop_document(request.session_id, table_type)
            raise

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

    row_html_out = HTMLParser.apply_tracking_to_row_html(
        new_row_html, [request.item_id], [], False
    )
    return {
        "success": True,
        "original_value": original_value,
        "new_value": request.new_value,
        "row_id": row_id,
        "row_html": row_html_out
    }


@router.post("/item/add")
async def add_item_to_cell(request: AddItemRequest):
    """
    Add a new item to a cell.

    Supports adding values to both single-value and multi-value fields:
    - Empty cells (any field type): Initializes the cell with the value
    - Non-empty multi-value fields: Appends the value with appropriate separator
    - Non-empty single-value fields: Raises error (defensive, UI prevents this)

    Can add either an empty item (for editing later) or a value directly.
    For adding with value, uses row_id and field_name parameters.
    For adding empty item (backward compatibility), uses item_id parameter.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    if request.new_value is not None and request.row_id and request.field_name:
        # ── Adding with value directly ─────────────────────────────────────
        field_name = request.field_name
        is_multi_value = field_name in HTMLParser.ITEM_SEPARATORS

        async with document_cache.session_lock(request.session_id):
            doc = await _get_editable_doc(request.session_id, session)
            try:
                soup = await doc.ensure_soup()
                row = soup.find('tr', id=request.row_id)
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail=f"Row '{request.row_id}' not found")

                has_value, _container_count = HTMLParser.get_cell_state_in_row(
                    row, field_name
                )

                pre_row_html = doc.row_html_or_none(request.row_id)
                if pre_row_html is None:
                    raise HTTPException(status_code=500,
                                        detail=f"Row '{request.row_id}' not found in document")

                await SessionManager.push_undo_row_snapshot(
                    request.session_id, table_type, request.row_id, pre_row_html
                )

                if not has_value:
                    # Path 1: Empty cell (any field type) → clear_cell + set value
                    new_item_id = HTMLParser.clear_cell_in_soup(soup, row, field_name)
                    if not new_item_id:
                        raise HTTPException(status_code=404, detail="Failed to initialize cell")
                    HTMLParser.update_item_value_in_soup(soup, new_item_id, request.new_value)
                elif not is_multi_value:
                    # Path 2: Non-empty single-value field → error (defensive)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot add to single-value field '{field_name}' that already has a value"
                    )
                else:
                    # Path 3: Non-empty multi-value field → append with separator
                    new_item_id = HTMLParser.add_item_in_cell(
                        soup, row, field_name, request.new_value
                    )

                new_row_html = doc.commit_row(request.row_id)
                await doc.persist()
            except HTTPException:
                raise
            except Exception:
                document_cache.drop_document(request.session_id, table_type)
                raise

            # Mark the new item as added
            edit_states = await SessionManager.load_edit_state(request.session_id)
            edit_states[new_item_id] = EditState(
                item_id=new_item_id,
                original_value='',
                edited_value=request.new_value,
                added=True,
                edited=False
            )
            await SessionManager.save_edit_state(request.session_id, edit_states)

            session.mark_edited()
            await SessionManager.save_session(session)

        row_html_out = HTMLParser.apply_tracking_to_row_html(
            new_row_html, [], [new_item_id], False
        )
        return {
            "success": True,
            "new_item_id": new_item_id,
            "row_id": request.row_id,
            "row_html": row_html_out
        }

    elif request.item_id:
        # ── Path 4: Backward compatibility - adding empty item ────────────
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

        row_id = _row_id_for_item(request.item_id)

        async with document_cache.session_lock(request.session_id):
            doc = await _get_editable_doc(request.session_id, session)
            try:
                soup = await doc.ensure_soup()
                row = soup.find('tr', id=row_id)
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail=f"Item '{request.item_id}' not found in HTML")

                pre_row_html = doc.row_html_or_none(row_id)
                if pre_row_html is None:
                    raise HTTPException(status_code=500,
                                        detail=f"Row '{row_id}' not found in document")

                await SessionManager.push_undo_row_snapshot(
                    request.session_id, table_type, row_id, pre_row_html
                )

                new_item_id = HTMLParser.add_item_in_cell(soup, row, field_name, '')

                new_row_html = doc.commit_row(row_id)
                await doc.persist()
            except HTTPException:
                raise
            except Exception:
                document_cache.drop_document(request.session_id, table_type)
                raise

            session.mark_edited()
            await SessionManager.save_session(session)

        row_html_out = HTMLParser.apply_tracking_to_row_html(
            new_row_html, [], [new_item_id], False
        )
        return {
            "success": True,
            "new_item_id": new_item_id,
            "row_id": row_id,
            "row_html": row_html_out
        }
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide either (row_id, field_name, new_value) or item_id"
        )


@router.delete("/item")
async def delete_item(request: DeleteItemRequest):
    """
    Delete a specific item from a multi-value cell.

    Removes the item-container with the given item_id.  If there are multiple
    items, separator cosmetics are handled by the frontend row replacement.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)
    row_id = _row_id_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        doc = await _get_editable_doc(request.session_id, session)
        try:
            soup = await doc.ensure_soup()

            # Capture original value before deletion for ghost overlay
            original_value = HTMLParser.get_item_value_from_soup(soup, request.item_id)

            # Parse item_id to get row_id and field_name
            parts = request.item_id.split('-')
            if len(parts) >= 3:
                # Save deleted item state for ghost overlay (before the
                # mutation, so the undo snapshot captures the pre-delete state)
                deleted_items = await SessionManager.load_deleted_item_state(request.session_id)
                deleted_items[request.item_id] = DeletedItemState(
                    item_id=request.item_id,
                    original_value=original_value or '',
                    row_id=parts[0],
                    field_name='-'.join(parts[1:-1])
                )
                await SessionManager.save_deleted_item_state(request.session_id, deleted_items)

            pre_row_html = doc.row_html_or_none(row_id)
            if pre_row_html is None:
                raise HTTPException(status_code=500,
                                    detail=f"Row '{row_id}' not found in document")

            await SessionManager.push_undo_row_snapshot(
                request.session_id, table_type, row_id, pre_row_html
            )

            HTMLParser.remove_item_in_soup(soup, request.item_id)

            new_row_html = doc.commit_row(row_id)
            await doc.persist()
        except HTTPException:
            raise
        except Exception:
            document_cache.drop_document(request.session_id, table_type)
            raise

        # Remove edit tracking for the deleted item
        edit_states = await SessionManager.load_edit_state(request.session_id)
        if request.item_id in edit_states:
            del edit_states[request.item_id]
            await SessionManager.save_edit_state(request.session_id, edit_states)

        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "item_id": request.item_id,
        "row_id": row_id,
        "row_html": new_row_html
    }


@router.post("/row/delete")
async def delete_row(request: DeleteRowRequest):
    """
    Delete an entire table row from the individual HTML file.

    The row is identified by its ``<tr id="rowN">`` attribute.  After deletion
    user should re-validate to export updated table without this row.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await _get_editable_doc(request.session_id, session)
        try:
            soup = await doc.ensure_soup()
            row = soup.find('tr', id=request.row_id)
            if row is None:
                # Nothing to delete — behave as a success no-op (the row is
                # already gone; the frontend removes its <tr> locally too).
                return {"success": True, "row_id": request.row_id, "removed": False}

            # Capture all item values in the row before deletion (single pass)
            row_items = HTMLParser.get_row_items_with_values(row)
            deleted_items = await SessionManager.load_deleted_item_state(request.session_id)
            parts_by_item = {}
            for item_id in row_items:
                parts = item_id.split('-')
                if len(parts) >= 3:
                    parts_by_item[item_id] = parts
                    deleted_items[item_id] = DeletedItemState(
                        item_id=item_id,
                        original_value=row_items[item_id] or '',
                        row_id=parts[0],
                        field_name='-'.join(parts[1:-1])
                    )
            await SessionManager.save_deleted_item_state(request.session_id, deleted_items)

            pre_row_html = doc.row_html_or_none(request.row_id)
            if pre_row_html is None:
                raise HTTPException(status_code=500,
                                    detail=f"Row '{request.row_id}' not found in document")
            next_row_id = next_row_id_in_str(doc.canonical, request.row_id)

            await SessionManager.push_undo_row_snapshot(
                request.session_id, table_type, request.row_id,
                pre_row_html, next_row_id
            )

            doc.commit_row_removal(request.row_id)
            await doc.persist()
        except HTTPException:
            raise
        except Exception:
            document_cache.drop_document(request.session_id, table_type)
            raise

        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "row_id": request.row_id, "removed": True}


@router.post("/row/add")
async def add_row(request: AddRowRequest):
    """
    Add a new empty row to the table.

    The new row is appended at the end of the table and contains empty
    item-containers for each field.  The response carries the new row's HTML
    (with the 'added' highlight) so the frontend can insert it in place.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await _get_editable_doc(request.session_id, session)
        try:
            soup = await doc.ensure_soup()

            new_row_id = HTMLParser.add_row_to_soup(soup)
            if not new_row_id:
                raise HTTPException(status_code=500, detail="Failed to add new row")

            # Undo entry for an added row: the row did not exist before
            # (pre_row_html=None → undo removes it again)
            await SessionManager.push_undo_row_snapshot(
                request.session_id, table_type, new_row_id, None, None
            )

            new_row_html = doc.commit_row_append(new_row_id)
            await doc.persist()
        except HTTPException:
            raise
        except Exception:
            document_cache.drop_document(request.session_id, table_type)
            raise

        # Mark the new row as added
        row_change_states = await SessionManager.load_row_change_state(request.session_id)
        row_change_states[new_row_id] = RowChangeState(
            row_id=new_row_id,
            added=True,
            deleted=False
        )
        await SessionManager.save_row_change_state(request.session_id, row_change_states)

        session.mark_edited()
        await SessionManager.save_session(session)

    row_html_out = HTMLParser.apply_tracking_to_row_html(
        new_row_html, [], [], True
    )
    return {
        "success": True,
        "row_id": new_row_id,
        "row_html": row_html_out
    }


@router.post("/cell/clear")
async def clear_cell_route(request: ClearCellRequest):
    """
    Clear all values from a single cell, leaving one empty item-container.

    Works for both multi-value and single-value fields.  Also serves as the
    "initialise" endpoint for cells that currently have no item-containers at
    all (e.g. a field that was empty in the original CSV).

    Tracks cleared items for ghost overlays, like delete-item/delete-row.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await _get_editable_doc(request.session_id, session)
        try:
            soup = await doc.ensure_soup()
            row = soup.find('tr', id=request.row_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Cell '{request.field_name}' not found in row '{request.row_id}'"
                )

            # Get all item ids/values in the cell before clearing (single pass)
            cell_items = HTMLParser.get_cell_items_with_values(row, request.field_name)
            deleted_items = await SessionManager.load_deleted_item_state(request.session_id)
            for item_id, value in cell_items.items():
                parts = item_id.split('-')
                if len(parts) >= 3:
                    deleted_items[item_id] = DeletedItemState(
                        item_id=item_id,
                        original_value=value or '',
                        row_id=parts[0],
                        field_name='-'.join(parts[1:-1])
                    )
            await SessionManager.save_deleted_item_state(request.session_id, deleted_items)

            pre_row_html = doc.row_html_or_none(request.row_id)
            if pre_row_html is None:
                raise HTTPException(status_code=500,
                                    detail=f"Row '{request.row_id}' not found in document")

            await SessionManager.push_undo_row_snapshot(
                request.session_id, table_type, request.row_id, pre_row_html
            )

            new_item_id = HTMLParser.clear_cell_in_soup(soup, row, request.field_name)
            if not new_item_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"Cell '{request.field_name}' not found in row '{request.row_id}'"
                )

            new_row_html = doc.commit_row(request.row_id)
            await doc.persist()
        except HTTPException:
            raise
        except Exception:
            document_cache.drop_document(request.session_id, table_type)
            raise

        # Remove edit tracking for all cleared items
        edit_states = await SessionManager.load_edit_state(request.session_id)
        for item_id in cell_items:
            if item_id in edit_states:
                del edit_states[item_id]
        await SessionManager.save_edit_state(request.session_id, edit_states)

        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "new_item_id": new_item_id,
        "row_id": request.row_id,
        "row_html": new_row_html
    }


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
      4. Use the *returned* report path to call ``_generate_html``.
      5. Save the new HTML back to the individual file (via ``save_html``,
         which also refreshes the document cache).

    For paired sessions (metadata + citations):
      1. Load both individual HTMLs.
      2. Parse and export each to a separate temp CSV.
      3. Run ``ValidatorService.validate_pair`` (ClosureValidator).
      4. Regenerate both individual HTMLs from their respective new reports.
      5. Merge the two individual HTMLs and save the result as the display file.

    CPU-heavy steps (validator + ``make_gui``) run in a worker thread so the
    event loop stays responsive.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    verify_id = (request.verify_id_existence
                 if request.verify_id_existence is not None
                 else session.verify_id_existence)

    session_dir = TEMP_DIR / request.session_id

    async with document_cache.session_lock(request.session_id):
        try:
            if session.has_metadata and session.has_citations:
                # ── Paired re-validation ────────────────────────────────────
                meta_doc = await document_cache.get_document(request.session_id, 'meta')
                cits_doc = await document_cache.get_document(request.session_id, 'cits')
                if meta_doc is None or cits_doc is None:
                    raise HTTPException(status_code=404,
                                        detail="Individual table HTML not found")

                try:
                    meta_rows = await asyncio.to_thread(
                        HTMLParser.parse_table_from_soup,
                        await meta_doc.ensure_soup()
                    )
                    cits_rows = await asyncio.to_thread(
                        HTMLParser.parse_table_from_soup,
                        await cits_doc.ensure_soup()
                    )
                except ValueError:
                    # A zero-error table renders without a <table> element
                    # (see _make_no_errors_html) — it cannot have been edited,
                    # so its data is unchanged: feed the original CSV back in.
                    meta_rows = None
                    cits_rows = None

                if meta_rows is not None and not meta_rows:
                    raise ValueError("No data found in metadata HTML table")
                if cits_rows is not None and not cits_rows:
                    raise ValueError("No data found in citations HTML table")

                # Export edited rows back to temporary CSV files (or reuse the
                # original CSV for table-less — i.e. unedited/valid — sides)
                temp_meta_csv = session_dir / 'temp_meta_revalidate.csv'
                temp_cits_csv = session_dir / 'temp_cits_revalidate.csv'

                if meta_rows is not None:
                    meta_csv_content = await asyncio.to_thread(
                        CSVExporter.rows_to_csv, meta_rows, session.meta_csv_path)
                    with open(temp_meta_csv, 'w', encoding='utf-8', newline='') as f:
                        f.write(meta_csv_content)
                else:
                    temp_meta_csv = Path(session.meta_csv_path)

                if cits_rows is not None:
                    cits_csv_content = await asyncio.to_thread(
                        CSVExporter.rows_to_csv, cits_rows, session.cits_csv_path)
                    with open(temp_cits_csv, 'w', encoding='utf-8', newline='') as f:
                        f.write(cits_csv_content)
                else:
                    temp_cits_csv = Path(session.cits_csv_path)

                # Run paired validation via ClosureValidator (CPU-bound → thread)
                meta_is_valid, cits_is_valid, meta_report_path, cits_report_path = \
                    await asyncio.to_thread(
                        ValidatorService.validate_pair,
                        meta_csv_path=str(temp_meta_csv),
                        cits_csv_path=str(temp_cits_csv),
                        meta_output_dir=str(session_dir),
                        cits_output_dir=str(session_dir),
                        verify_id_existence=verify_id
                    )

                # Regenerate individual HTML files
                meta_table_path = session_dir / 'meta_table.html'
                cits_table_path = session_dir / 'cits_table.html'

                await asyncio.to_thread(_generate_html, str(temp_meta_csv),
                                        meta_report_path, str(meta_table_path),
                                        meta_is_valid)
                await asyncio.to_thread(_generate_html, str(temp_cits_csv),
                                        cits_report_path, str(cits_table_path),
                                        cits_is_valid)

                with open(meta_table_path, 'r', encoding='utf-8', newline='') as f:
                    new_meta_html = f.read()
                with open(cits_table_path, 'r', encoding='utf-8', newline='') as f:
                    new_cits_html = f.read()

                await SessionManager.save_html(request.session_id, new_meta_html, 'meta')
                await SessionManager.save_html(request.session_id, new_cits_html, 'cits')

                # Re-merge and save as display file (compatibility; the served
                # display is derived, but keep the merged file on disk)
                merged_path = session_dir / 'meta_html.html'
                await asyncio.to_thread(merge_html_files, str(meta_table_path),
                                        str(cits_table_path), str(merged_path))
                with open(merged_path, 'r', encoding='utf-8', newline='') as f:
                    merged_content = f.read()
                await SessionManager.save_html(request.session_id, merged_content, 'display')

                # Update baseline snapshots for deletion detection
                await SessionManager.save_baseline_snapshot(request.session_id, new_meta_html, 'meta')
                await SessionManager.save_baseline_snapshot(request.session_id, new_cits_html, 'cits')

                # Update session report paths
                session.meta_report_path = meta_report_path
                session.cits_report_path = cits_report_path

                total_error_count = (len(load_jsonl_report(meta_report_path))
                                     + len(load_jsonl_report(cits_report_path)))

                # Clean up temp files (never the reused original CSVs)
                if temp_meta_csv.name.startswith('temp_'):
                    temp_meta_csv.unlink(missing_ok=True)
                if temp_cits_csv.name.startswith('temp_'):
                    temp_cits_csv.unlink(missing_ok=True)

            else:
                # ── Single-table re-validation ──────────────────────────────
                table_type = _editable_table_type(session)
                doc = await document_cache.get_document(request.session_id, table_type)
                if doc is None:
                    raise HTTPException(status_code=404, detail="HTML content not found")

                try:
                    rows_data = await asyncio.to_thread(
                        HTMLParser.parse_table_from_soup,
                        await doc.ensure_soup()
                    )
                except Exception as e:
                    raise ValueError(f"Failed to parse HTML table: {e}")

                if not rows_data:
                    raise ValueError("No data found in HTML table")

                original_csv_path = (session.meta_csv_path if session.has_metadata
                                     else session.cits_csv_path)
                csv_content = await asyncio.to_thread(
                    CSVExporter.rows_to_csv, rows_data, original_csv_path)

                temp_csv_path = session_dir / 'temp_revalidate.csv'
                with open(temp_csv_path, 'w', encoding='utf-8', newline='') as f:
                    f.write(csv_content)

                # Run validation (CPU-bound → thread).  The report path is
                # taken from validator.output_fp_json, so it is always the
                # file that was *just* written.
                is_valid, report_path = await asyncio.to_thread(
                    ValidatorService.validate_single,
                    csv_path=str(temp_csv_path),
                    output_dir=str(session_dir),
                    verify_id_existence=verify_id
                )

                # Generate new HTML using the freshly written report
                temp_html_path = session_dir / 'temp_revalidate.html'
                await asyncio.to_thread(_generate_html, str(temp_csv_path),
                                        report_path, str(temp_html_path), is_valid)

                with open(temp_html_path, 'r', encoding='utf-8', newline='') as f:
                    new_html = f.read()

                # Save updated individual HTML (grey highlights intentionally
                # dropped — re-validation is the canonical "accept and re-check"
                # action; edited items are no longer specially marked afterwards).
                await SessionManager.save_html(request.session_id, new_html, table_type)

                # Update baseline snapshot for deletion detection
                await SessionManager.save_baseline_snapshot(request.session_id, new_html, table_type)

                # Update session report path
                if session.has_metadata:
                    session.meta_report_path = report_path
                else:
                    session.cits_report_path = report_path

                total_error_count = len(load_jsonl_report(report_path))

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

    # Issue filtering scopes to the first table (same as the old behaviour on
    # the merged display file, whose first table is the metadata table).
    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await document_cache.get_document(request.session_id, table_type)
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        soup = await doc.ensure_soup()
        row_indices = HTMLParser.get_rows_by_issue_in_soup(soup, request.issue_id)
        filtered_html = HTMLParser.build_filtered_table_html(soup, row_indices)

    edit_states = await SessionManager.load_edit_state(request.session_id)
    if edit_states:
        edited_ids = [item_id for item_id, state in edit_states.items() if state.edited]
        if edited_ids:
            filtered_html = _mark_tracked_rows(filtered_html, edited_ids, [], [])

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


@router.get("/deleted/{session_id}")
async def get_deleted_view(session_id: str):
    """
    Get HTML content with ghost overlays showing deleted items and rows.

    Compares the baseline (post-validation) tree with the current tree, then
    splices ghost elements into a copy of the current canonical string (the
    cached tree itself is never mutated by a view).

    Note: for paired sessions the display baseline was never persisted by any
    code path, so — as before this optimisation — ghost overlays are only
    available for single-table sessions.
    """
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.has_metadata and session.has_citations:
        # Paired: same historical behaviour — no persisted display baseline,
        # so no ghost overlays.
        meta_doc = await document_cache.get_document(session_id, 'meta')
        cits_doc = await document_cache.get_document(session_id, 'cits')
        if meta_doc is None or cits_doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")
        return {"html": compose_display(meta_doc.canonical, cits_doc.canonical),
                "has_ghosts": False}

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(session_id):
        doc = await document_cache.get_document(session_id, table_type)
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")

        baseline_doc = await document_cache.get_document(
            session_id, f'baseline_{table_type}')
        if baseline_doc is None:
            # No baseline exists - return current HTML without ghost overlays
            # (happens when no validation has been performed yet)
            return {"html": doc.canonical, "has_ghosts": False}

        await doc.ensure_soup()
        await baseline_doc.ensure_soup()
        current_html = doc.canonical  # read after ensure (may canonicalize)

        # Identify deletions with values (O(rows + items))
        deletions = await asyncio.to_thread(
            HTMLParser.identify_deletions_fast, baseline_doc.soup, doc.soup
        )

        # Combine with deleted item states from the tracking store
        deleted_items_db = await SessionManager.load_deleted_item_state(session_id)
        deleted_item_values = deletions.get('deleted_item_values', {})
        for item_id, state in deleted_items_db.items():
            if item_id not in deleted_item_values:
                deleted_item_values[item_id] = state.original_value

        if not deletions.get('deleted_items') and not deletions.get('deleted_rows'):
            return {"html": current_html, "has_ghosts": False}

        html_with_ghosts = HTMLParser.insert_deleted_overlays_fast(
            current_html, doc.soup, deletions, deleted_item_values
        )

    return {
        "html": html_with_ghosts,
        "has_ghosts": True,
        "deleted_items_count": len(deletions.get('deleted_items', [])),
        "deleted_rows_count": len(deletions.get('deleted_rows', []))
    }


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


# ---------------------------------------------------------------------------
# Undo / Redo endpoints
# ---------------------------------------------------------------------------

@router.get("/undo_state/{session_id}")
async def get_undo_state(session_id: str):
    """Return whether undo and redo are currently available for this session."""
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    table_type = _editable_table_type(session)
    return await SessionManager.get_undo_availability(session_id, table_type)


@router.post("/undo")
async def undo(request: UndoRedoRequest):
    """
    Undo the last mutation (row-level snapshot restore).

    Restores the affected ``<tr>`` to its pre-mutation state and rolls back
    the tracking sidecars (edit_state, row_change_state, deleted_item_state).
    The post-mutation image is pushed onto the redo stack.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await document_cache.get_document(request.session_id, table_type)
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")

        entry = await SessionManager.pop_undo_row_snapshot(
            request.session_id, table_type, doc
        )
        if entry is None:
            avail = await SessionManager.get_undo_availability(request.session_id, table_type)
            return {"success": False, "message": "Nothing to undo", **avail}

        session.mark_edited()
        await SessionManager.save_session(session)
        avail = await SessionManager.get_undo_availability(request.session_id, table_type)

    return {"success": True, **avail}


@router.post("/redo")
async def redo(request: UndoRedoRequest):
    """
    Redo the last undone mutation (row-level snapshot restore).
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)

    async with document_cache.session_lock(request.session_id):
        doc = await document_cache.get_document(request.session_id, table_type)
        if doc is None:
            raise HTTPException(status_code=404, detail="HTML content not found")

        entry = await SessionManager.pop_redo_row_snapshot(
            request.session_id, table_type, doc
        )
        if entry is None:
            avail = await SessionManager.get_undo_availability(request.session_id, table_type)
            return {"success": False, "message": "Nothing to redo", **avail}

        session.mark_edited()
        await SessionManager.save_session(session)
        avail = await SessionManager.get_undo_availability(request.session_id, table_type)

    return {"success": True, **avail}
