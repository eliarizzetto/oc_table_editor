"""Edit operations routes — event-sourced.

Mutations append normalized events to the session's change journal
(``services/journal.py``) and return the affected row's HTML (computed by
``services/view_builder.py`` from the immutable baseline + replayed events)
so the frontend can patch a single ``<tr>`` in place.  No route parses a
whole document or writes a 12 MB file: the only full parses happen inside
upload/revalidate (artifact building), and the only big writes happen on
Save (commit) and revalidate.
"""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services import SessionManager, HTMLParser, ValidatorService, CSVExporter
from services.journal import ChangeJournal
from services.session_document import (
    SpliceError,
    _slice_div,
    compose_display,
    document_cache,
)
from services.validator_service import load_jsonl_report
from services.view_builder import (
    TableView,
    build_generation_artifacts,
    load_journal_view,
    load_table_state,
)
from models import Session
from config import TABLE_PAGE_SIZE, TEMP_DIR

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
    """The table the journal edits ('meta' or 'cits')."""
    return 'meta' if session.has_metadata else 'cits'


def _row_id_for_item(item_id: str) -> str:
    """'rowN' for an item id '{N}-{field}-{idx}'."""
    return f"row{item_id.split('-')[0]}"


def _field_for_item(item_id: str) -> str:
    parts = item_id.split('-')
    return '-'.join(parts[1:-1]) if len(parts) >= 3 else ''


async def _load_view(session_id: str, session: Session):
    """Load (journal, view, table_state) for the editable table.

    Caller must hold the session lock.  Raises 404 when the baseline
    (base) is missing.
    """
    loaded = await load_journal_view(session_id, _editable_table_type(session))
    if loaded is None:
        raise HTTPException(status_code=404, detail="HTML content not found")
    return loaded


def _recompute(state: dict, journal: ChangeJournal) -> TableView:
    return TableView(state['base_html'], state['artifacts'],
                     journal.applied_events)


def _cleanup_legacy_state_files(session_id: str) -> None:
    """Remove pre-journal tracking files if a session still carries them."""
    session_dir = TEMP_DIR / session_id
    for name in ('edit_state.json', 'row_change_state.json',
                 'deleted_item_state.json', 'undo_state.json'):
        (session_dir / name).unlink(missing_ok=True)
    import shutil
    shutil.rmtree(session_dir / 'undo', ignore_errors=True)


def _page_count(total: int) -> int:
    """Number of TABLE_PAGE_SIZE pages needed for ``total`` rows (≥ 1)."""
    return max(1, -(-total // TABLE_PAGE_SIZE))


def _pager_html(kind: str, page: int, page_count: int, total_rows: int) -> str:
    """Pager bar injected after each table.  Clicks are handled by the
    delegated listener in editor.js (``[data-pager] button[data-page]``)."""
    at_first = page <= 1
    at_last = page >= page_count
    rows_label = 'row' if total_rows == 1 else 'rows'
    return (
        f'<div class="table-pager" data-pager="{kind}">'
        f'<button type="button" class="btn btn-sm btn-outline-secondary" '
        f'data-page="1" title="First page"{" disabled" if at_first else ""}>«</button>'
        f'<button type="button" class="btn btn-sm btn-outline-secondary" '
        f'data-page="{max(1, page - 1)}" title="Previous page"{" disabled" if at_first else ""}>‹</button>'
        f'<span class="pager-info">Page {page} of {page_count} · {total_rows} {rows_label}</span>'
        f'<button type="button" class="btn btn-sm btn-outline-secondary" '
        f'data-page="{min(page_count, page + 1)}" title="Next page"{" disabled" if at_last else ""}>›</button>'
        f'<button type="button" class="btn btn-sm btn-outline-secondary" '
        f'data-page="{page_count}" title="Last page"{" disabled" if at_last else ""}>»</button>'
        f'</div>'
    )


def _general_info_or_500(base_html: str, table_type: str) -> str:
    try:
        return _slice_div(base_html, 'container-fluid general-info')
    except SpliceError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot slice general-info from {table_type} baseline: {exc}")


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


class CommitRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/table/{session_id}")
async def get_table_view(session_id: str, page: int = 1, show_all: bool = False,
                         changes_only: bool = False, issue_id: Optional[str] = None,
                         focus_row_id: Optional[str] = None, cits_page: int = 1):
    """
    One page (≤ TABLE_PAGE_SIZE rows) of the editor's table view.

    Row visibility: by default only rows with issues (errors/warnings) or
    with journal changes (edits / additions / deletions — ghost rows count
    as changed); ``show_all`` reveals every row regardless of validity,
    ``changes_only`` restricts the view to changed rows.  ``issue_id``
    switches to the single-issue filtered view.  Ghost overlays for deleted
    items/rows are always part of the rendered rows.  Rows are selected by
    id, never by position; ``focus_row_id`` (a live row id, or a
    ``ghost-`` prefixed one) selects the page containing that row.

    For paired sessions the read-only citations table is appended, paginated
    independently via ``cits_page``.
    """
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(session_id):
        journal, view, state = await _load_view(session_id, session)
        artifacts = state['artifacts']

        if not artifacts.get('has_table'):
            # Legacy fallback: fully-valid uploads have no parsable table —
            # serve the whole document with the paging UI disabled.
            if session.has_metadata and session.has_citations:
                cits_state = await load_table_state(session_id, 'cits')
                if cits_state is None:
                    raise HTTPException(status_code=404,
                                        detail="HTML content not found")
                html_content = compose_display(view.html, cits_state['base_html'])
            else:
                html_content = view.html
            return {"html": html_content, "has_table": False,
                    "paginated": False, "page": 1, "page_count": 1,
                    "total_rows": 0, "table_type": _editable_table_type(session),
                    "cits": None}

        deletions = view.compute_deletions()
        changed = view.changed_row_ids(deletions)
        issues = view.issue_row_ids()

        if issue_id is not None:
            entries = [{'row_id': rid, 'ghost': False}
                       for rid in artifacts['issue_index'].get(issue_id, [])
                       if rid in view.row_ids]
        else:
            entries = [e for e in view.display_rows()
                       if (show_all or e['row_id'] in issues
                           or e['row_id'] in changed)
                       and (not changes_only or e['row_id'] in changed)]

        total = len(entries)
        page_count = _page_count(total)
        page = max(1, min(page, page_count))
        if focus_row_id:
            base_focus = (focus_row_id[6:]
                          if focus_row_id.startswith('ghost-') else focus_row_id)
            for i, e in enumerate(entries):
                if e['row_id'] == base_focus:
                    page = i // TABLE_PAGE_SIZE + 1
                    break

        page_entries = entries[(page - 1) * TABLE_PAGE_SIZE:page * TABLE_PAGE_SIZE]

        # Deleted items per surviving row (for in-row ghost containers)
        deleted_by_row = {}
        for item_id in deletions['deleted_items']:
            rid = _row_id_for_item(item_id)
            if rid in deletions['deleted_rows']:
                continue
            deleted_by_row.setdefault(rid, []).append(item_id)
        values = deletions['deleted_item_values']

        row_parts = []
        for e in page_entries:
            if e['ghost']:
                row_parts.append(view.ghost_row_html(e['row_id']))
            else:
                row_parts.append(view.row_html_with_ghosts(
                    e['row_id'], deleted_by_row.get(e['row_id'], []), values))

        editable_open = artifacts['table_open_tag'].replace(
            '>', ' data-editable="1">', 1)
        html_parts = [
            _general_info_or_500(state['base_html'],
                                 _editable_table_type(session)),
            '<div class="table-container container-fluid">',
            editable_open, artifacts['thead_html'], '<tbody>',
            *row_parts,
            '</tbody></table></div>',
            _pager_html('meta', page, page_count, total),
        ]

        cits_info = None
        if session.has_metadata and session.has_citations:
            cits_state = await load_table_state(session_id, 'cits')
            if cits_state is None:
                raise HTTPException(status_code=404,
                                    detail="HTML content not found")
            cits_art = cits_state['artifacts']
            cits_base = cits_state['base_html']
            if cits_art.get('has_table'):
                cits_row_ids = cits_art.get('row_ids', [])
                cits_total = len(cits_row_ids)
                cits_page_count = _page_count(cits_total)
                cits_page = max(1, min(cits_page, cits_page_count))
                cits_slice = cits_row_ids[(cits_page - 1) * TABLE_PAGE_SIZE:
                                          cits_page * TABLE_PAGE_SIZE]
                cits_rows = []
                for rid in cits_slice:
                    s, e = cits_art['row_offsets'][rid]
                    cits_rows.append(cits_base[s:e])
                try:
                    cits_gi = _slice_div(cits_base, 'container-fluid general-info')
                except SpliceError:
                    cits_gi = ''
                html_parts.extend([
                    cits_gi,
                    '<div class="table-container container-fluid">',
                    cits_art['table_open_tag'], cits_art['thead_html'], '<tbody>',
                    *cits_rows,
                    '</tbody></table></div>',
                    _pager_html('cits', cits_page, cits_page_count, cits_total),
                ])
                cits_info = {"page": cits_page, "page_count": cits_page_count,
                             "total_rows": cits_total}
            else:
                cits_info = {"page": 1, "page_count": 1, "total_rows": 0}

    return {"html": ''.join(html_parts), "has_table": True, "paginated": True,
            "page": page, "page_count": page_count, "total_rows": total,
            "table_type": _editable_table_type(session), "cits": cits_info}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item — appends a ``set_item`` (or ``remove_item`` when a
    multi-value item is emptied, mirroring the auto-remove behaviour) event.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)
    row_id = _row_id_for_item(request.item_id)
    field_name = _field_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        bs, row = view.row_soup(row_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Item '{request.item_id}' not found")

        original_value = HTMLParser.get_item_value_from_soup(bs, request.item_id)
        if original_value is None:
            raise HTTPException(status_code=404,
                                detail=f"Item '{request.item_id}' not found")

        is_multi_value = field_name in HTMLParser.ITEM_SEPARATORS
        if is_multi_value and request.new_value.strip() == '':
            # Edit-to-empty on a multi-value field is a removal (no stray
            # separators in the exported CSV; ghost semantics rely on it).
            await journal.append('remove_item', row=row_id,
                                 item=request.item_id, field=field_name)
        else:
            await journal.append('set_item', row=row_id,
                                 item=request.item_id, field=field_name,
                                 value=request.new_value)

        new_view = _recompute(state, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "original_value": original_value,
        "new_value": request.new_value,
        "row_id": row_id,
        "row_html": new_view.row_html(row_id)
    }


@router.post("/item/add")
async def add_item_to_cell(request: AddItemRequest):
    """
    Add a new item to a cell — appends an ``init_cell`` (empty cell) or
    ``append_item`` (non-empty multi-value cell) event.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if request.new_value is not None and request.row_id and request.field_name:
        # ── Adding with value directly ─────────────────────────────────────
        field_name = request.field_name
        is_multi_value = field_name in HTMLParser.ITEM_SEPARATORS

        async with document_cache.session_lock(request.session_id):
            journal, view, state = await _load_view(request.session_id, session)
            bs, row = view.row_soup(request.row_id)
            if row is None:
                raise HTTPException(status_code=404,
                                    detail=f"Row '{request.row_id}' not found")

            has_value, _ = HTMLParser.get_cell_state_in_row(row, field_name)

            if not has_value:
                new_item_id = f"{request.row_id[3:]}-{field_name}-0"
                await journal.append('init_cell', row=request.row_id,
                                     field=field_name, value=request.new_value)
            elif not is_multi_value:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot add to single-value field '{field_name}' "
                           f"that already has a value"
                )
            else:
                new_item_id = HTMLParser.next_item_id_in_cell(row, field_name)
                if not new_item_id:
                    raise HTTPException(status_code=404,
                                        detail=f"Field '{field_name}' not found")
                await journal.append('append_item', row=request.row_id,
                                     field=field_name, value=request.new_value)

            new_view = _recompute(state, journal)
            session.mark_edited()
            await SessionManager.save_session(session)

        return {
            "success": True,
            "new_item_id": new_item_id,
            "row_id": request.row_id,
            "row_html": new_view.row_html(request.row_id)
        }

    elif request.item_id:
        # ── Backward compatibility: adding an empty item ───────────────────
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
            journal, view, state = await _load_view(request.session_id, session)
            bs, row = view.row_soup(row_id)
            if row is None:
                raise HTTPException(status_code=404,
                                    detail=f"Item '{request.item_id}' not found in HTML")
            new_item_id = HTMLParser.next_item_id_in_cell(row, field_name)
            if not new_item_id:
                raise HTTPException(status_code=404,
                                    detail=f"Item '{request.item_id}' not found in HTML")
            await journal.append('append_item', row=row_id,
                                 field=field_name, value='')
            new_view = _recompute(state, journal)
            session.mark_edited()
            await SessionManager.save_session(session)

        return {
            "success": True,
            "new_item_id": new_item_id,
            "row_id": row_id,
            "row_html": new_view.row_html(row_id)
        }
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide either (row_id, field_name, new_value) or item_id"
        )


@router.delete("/item")
async def delete_item(request: DeleteItemRequest):
    """Delete a specific item from a multi-value cell (``remove_item``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    row_id = _row_id_for_item(request.item_id)
    field_name = _field_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        bs, row = view.row_soup(row_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Item '{request.item_id}' not found")
        if HTMLParser.get_item_value_from_soup(bs, request.item_id) is None:
            # Already absent — success no-op (matches the historical
            # lenient behaviour of remove_item).
            return {
                "success": True,
                "item_id": request.item_id,
                "row_id": row_id,
                "row_html": view.row_html(row_id)
            }

        await journal.append('remove_item', row=row_id,
                             item=request.item_id, field=field_name)
        new_view = _recompute(state, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "item_id": request.item_id,
        "row_id": row_id,
        "row_html": new_view.row_html(row_id)
    }


@router.post("/row/delete")
async def delete_row(request: DeleteRowRequest):
    """Delete an entire table row (``delete_row``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        if request.row_id not in view.row_ids:
            # Already gone — success no-op.
            return {"success": True, "row_id": request.row_id, "removed": False}

        await journal.append('delete_row', row=request.row_id)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "row_id": request.row_id, "removed": True}


@router.post("/row/add")
async def add_row(request: AddRowRequest):
    """Add a new empty row at the end of the table (``add_row``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        if not state['artifacts'].get('has_table'):
            raise HTTPException(status_code=500, detail="Failed to add new row")
        new_row_id = view.next_add_row_id()
        await journal.append('add_row', row=new_row_id)
        new_view = _recompute(state, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": new_row_id,
        "row_html": new_view.row_html(new_row_id)
    }


@router.post("/cell/clear")
async def clear_cell_route(request: ClearCellRequest):
    """Clear all values from a cell, leaving one empty item-container."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        bs, row = view.row_soup(request.row_id)
        if row is None or HTMLParser._get_cell_in_row(row, request.field_name) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Cell '{request.field_name}' not found in row '{request.row_id}'"
            )

        await journal.append('clear_cell', row=request.row_id,
                             field=request.field_name)
        new_view = _recompute(state, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    new_item_id = f"{request.row_id[3:]}-{request.field_name}-0"
    return {
        "success": True,
        "new_item_id": new_item_id,
        "row_id": request.row_id,
        "row_html": new_view.row_html(request.row_id)
    }


# ---------------------------------------------------------------------------
# Undo / Redo
# ---------------------------------------------------------------------------

@router.get("/undo_state/{session_id}")
async def get_undo_state(session_id: str):
    """Return whether undo and redo are currently available for this session."""
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    async with document_cache.session_lock(session_id):
        journal, _view, _state = await _load_view(session_id, session)
        return {"can_undo": journal.can_undo, "can_redo": journal.can_redo}


@router.post("/undo")
async def undo(request: UndoRedoRequest):
    """Undo the last mutation: move the journal cursor back one event."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journal, _view, state = await _load_view(request.session_id, session)
        ev = await journal.undo()
        if ev is None:
            return {"success": False, "message": "Nothing to undo",
                    "can_undo": journal.can_undo, "can_redo": journal.can_redo}
        new_view = _recompute(state, journal)
        payload = _patch_payload(ev, new_view, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "can_undo": journal.can_undo,
            "can_redo": journal.can_redo, **payload}


@router.post("/redo")
async def redo(request: UndoRedoRequest):
    """Redo the last undone mutation: move the journal cursor forward."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journal, _view, state = await _load_view(request.session_id, session)
        ev = await journal.redo()
        if ev is None:
            return {"success": False, "message": "Nothing to redo",
                    "can_undo": journal.can_undo, "can_redo": journal.can_redo}
        new_view = _recompute(state, journal)
        payload = _patch_payload(ev, new_view, journal)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "can_undo": journal.can_undo,
            "can_redo": journal.can_redo, **payload}


def _patch_payload(ev: dict, view: TableView, journal: ChangeJournal) -> dict:
    """Frontend patch descriptor for an undone/redone row event."""
    rid = ev['row']
    if ev['op'] in ('add_row', 'delete_row'):
        mode = 'insert' if rid in view.row_ids else 'remove'
    else:
        mode = 'replace'
    payload = {"mode": mode, "row_id": rid}
    if mode == 'replace':
        payload["row_html"] = view.row_html(rid)
    elif mode == 'insert':
        payload["row_html"] = view.row_html(rid)
        payload["insert_before_row_id"] = view.next_row_id_after(rid)
    return payload


# ---------------------------------------------------------------------------
# Commit (Save)
# ---------------------------------------------------------------------------

@router.post("/commit")
async def commit(request: CommitRequest):
    """
    Save: materialize the current view (baseline + events ≤ cursor) into the
    table file(s).  The journal and undo history are preserved — undo still
    steps back past the save.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = _editable_table_type(session)
    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session)
        await SessionManager.save_html(request.session_id, view.html, table_type)
        if session.has_metadata and session.has_citations:
            cits_state = await load_table_state(request.session_id, 'cits')
            if cits_state is not None:
                display = compose_display(view.html, cits_state['base_html'])
                await SessionManager.save_html(request.session_id, display, 'display')
        await journal.mark_saved()

    return {"success": True, "saved": True}


# ---------------------------------------------------------------------------
# Revalidate
# ---------------------------------------------------------------------------

@router.post("/revalidate")
async def revalidate(request: RevalidateRequest):
    """
    Re-run validation on the current (possibly edited) table data and
    regenerate the HTML so issue squares reflect the latest results.

    Rows come from the journal replay (view.rows_for_export) — no HTML
    parsing.  After regeneration the new baselines become the next
    generation's base, artifacts are rebuilt (one threaded parse), and the
    journal resets: undo history does not survive a revalidate (row ids are
    renumbered by ``make_gui``, so cross-generation undo was never sound).
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
                journal, meta_view, meta_state = await _load_view(
                    request.session_id, session)
                meta_rows = (meta_view.rows_for_export()
                             if meta_state['artifacts'].get('has_table') else None)

                cits_state = await load_table_state(request.session_id, 'cits')
                if cits_state is None:
                    raise HTTPException(status_code=404,
                                        detail="Citations baseline not found")
                cits_rows = None
                if cits_state['artifacts'].get('has_table'):
                    # Citations are not editable — base rows are current.
                    cits_view = TableView(cits_state['base_html'],
                                          cits_state['artifacts'], [])
                    cits_rows = cits_view.rows_for_export()

                if meta_rows is not None and not meta_rows:
                    raise ValueError("No data found in metadata HTML table")
                if cits_rows is not None and not cits_rows:
                    raise ValueError("No data found in citations HTML table")

                # Rows → CSV (reuse the original CSV when a side is table-less)
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

                meta_is_valid, cits_is_valid, meta_report_path, cits_report_path = \
                    await asyncio.to_thread(
                        ValidatorService.validate_pair,
                        meta_csv_path=str(temp_meta_csv),
                        cits_csv_path=str(temp_cits_csv),
                        meta_output_dir=str(session_dir),
                        cits_output_dir=str(session_dir),
                        verify_id_existence=verify_id
                    )

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

                merged_path = session_dir / 'meta_html.html'
                await asyncio.to_thread(merge_html_files, str(meta_table_path),
                                        str(cits_table_path), str(merged_path))
                with open(merged_path, 'r', encoding='utf-8', newline='') as f:
                    merged_content = f.read()
                await SessionManager.save_html(request.session_id, merged_content, 'display')

                await SessionManager.save_baseline_snapshot(
                    request.session_id, new_meta_html, 'meta')
                await SessionManager.save_baseline_snapshot(
                    request.session_id, new_cits_html, 'cits')
                gen_meta = await build_generation_artifacts(request.session_id, 'meta')
                await build_generation_artifacts(request.session_id, 'cits')
                await journal.reset(gen_meta, 'meta')

                session.meta_report_path = meta_report_path
                session.cits_report_path = cits_report_path
                total_error_count = (len(load_jsonl_report(meta_report_path))
                                     + len(load_jsonl_report(cits_report_path)))

                if temp_meta_csv.name.startswith('temp_'):
                    temp_meta_csv.unlink(missing_ok=True)
                if temp_cits_csv.name.startswith('temp_'):
                    temp_cits_csv.unlink(missing_ok=True)

            else:
                # ── Single-table re-validation ──────────────────────────────
                table_type = _editable_table_type(session)
                journal, view, state = await _load_view(request.session_id, session)
                rows_data = (view.rows_for_export()
                             if state['artifacts'].get('has_table') else None)
                if rows_data is not None and not rows_data:
                    raise ValueError("No data found in HTML table")

                original_csv_path = (session.meta_csv_path if session.has_metadata
                                     else session.cits_csv_path)
                if rows_data is not None:
                    csv_content = await asyncio.to_thread(
                        CSVExporter.rows_to_csv, rows_data, original_csv_path)
                    temp_csv_path = session_dir / 'temp_revalidate.csv'
                    with open(temp_csv_path, 'w', encoding='utf-8', newline='') as f:
                        f.write(csv_content)
                else:
                    # Table-less (fully valid) document: data unchanged.
                    temp_csv_path = Path(original_csv_path)

                is_valid, report_path = await asyncio.to_thread(
                    ValidatorService.validate_single,
                    csv_path=str(temp_csv_path),
                    output_dir=str(session_dir),
                    verify_id_existence=verify_id
                )

                temp_html_path = session_dir / 'temp_revalidate.html'
                await asyncio.to_thread(_generate_html, str(temp_csv_path),
                                        report_path, str(temp_html_path), is_valid)
                with open(temp_html_path, 'r', encoding='utf-8', newline='') as f:
                    new_html = f.read()

                await SessionManager.save_html(request.session_id, new_html, table_type)
                await SessionManager.save_baseline_snapshot(
                    request.session_id, new_html, table_type)
                generation = await build_generation_artifacts(
                    request.session_id, table_type)
                await journal.reset(generation, table_type)

                if session.has_metadata:
                    session.meta_report_path = report_path
                else:
                    session.cits_report_path = report_path
                total_error_count = len(load_jsonl_report(report_path))

                if temp_csv_path.name.startswith('temp_'):
                    temp_csv_path.unlink(missing_ok=True)
                temp_html_path.unlink(missing_ok=True)

            _cleanup_legacy_state_files(request.session_id)

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


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Session information (counts, unsaved-changes flag, undo availability)."""
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(session_id):
        journal, view, _state = await _load_view(session_id, session)
        edited_count = len(view.edited_item_ids)
        unsaved = journal.has_unsaved_changes
        can_undo = journal.can_undo
        can_redo = journal.can_redo

    return {
        "session_id": session.session_id,
        "has_metadata": session.has_metadata,
        "has_citations": session.has_citations,
        "verify_id_existence": session.verify_id_existence,
        "has_edits_since_validation": session.has_edits_since_validation,
        "edited_items_count": edited_count,
        "unsaved_changes": unsaved,
        "can_undo": can_undo,
        "can_redo": can_redo,
        "last_validated_at": session.last_validated_at
    }
