"""Edit operations routes — event-sourced.

Mutations append normalized events to the session's change journals
(``services/journal.py``); the frontend then re-renders the current page
from ``GET /table/{session_id}`` (page-bounded views computed by
``services/view_builder.py`` from the immutable baseline + replayed
events).  No route parses a whole document or writes a 12 MB file: the
only full parses happen inside upload/revalidate (artifact building), and
the only big writes happen on Save (commit) and revalidate.
"""
import asyncio
import html
import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services import SessionManager, HTMLParser, ValidatorService, CSVExporter
from services.journal import ChangeJournal, truncate_redo_tails
from services.session_document import document_cache
from services.table_scope import (
    editable_table_type,
    resolve_table_type,
    session_table_types,
)
from services.validator_service import load_jsonl_report
from services.view_builder import (
    build_generation_artifacts,
    load_journal_view,
    load_table_state,
)
from models import Session
from config import TABLE_PAGE_SIZE, TEMP_DIR

# For counting CSV rows on the table-less fallback path
from oc_validator.helper import CSVStreamReader
from oc_validator.main import TableNotMatchingInstance

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_id_for_item(item_id: str) -> str:
    """'rowN' for an item id '{N}-{field}-{idx}'."""
    return f"row{item_id.split('-')[0]}"


def _field_for_item(item_id: str) -> str:
    parts = item_id.split('-')
    return '-'.join(parts[1:-1]) if len(parts) >= 3 else ''


async def _load_view(session_id: str, session: Session,
                    table_type: Optional[str] = None):
    """Load (journal, view, table_state) for one table (default: primary).

    Caller must hold the session lock.  Raises 404 when the baseline
    (base) is missing.
    """
    tt = resolve_table_type(session, table_type)
    loaded = await load_journal_view(session_id, tt)
    if loaded is None:
        raise HTTPException(status_code=404, detail="HTML content not found")
    return loaded


async def _append_event(session: Session, session_id: str, table_type: str,
                        journal: ChangeJournal, op: str, **fields) -> dict:
    """Append one event, keeping the session-wide undo stack coherent.

    Both tables share one logical undo stack (ordered by each event's
    ``gseq`` stamp), so a new edit must destroy the redo tails of EVERY
    journal — not just the one being appended to — before appending.
    Caller must hold the session lock."""
    await truncate_redo_tails(session_id, session_table_types(session),
                              exclude=(table_type,))
    return await journal.append(op, **fields)


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


@lru_cache(maxsize=128)
def _cached_report_counts(path: str, mtime_ns: int) -> tuple:
    """(errors, warnings) tally of a JSONL report, cached per file mtime so
    pagination clicks don't re-read the report (revalidate overwrites the
    same path; the mtime key invalidates the stale entry)."""
    entries = load_jsonl_report(path)
    return (sum(1 for e in entries if e.get('error_type') == 'error'),
            sum(1 for e in entries if e.get('error_type') == 'warning'))


def _report_counts(report_path: Optional[str]) -> tuple:
    """(errors, warnings) from a JSONL report; (0, 0) when missing/unreadable."""
    if not report_path:
        return 0, 0
    try:
        mtime_ns = Path(report_path).stat().st_mtime_ns
    except OSError:
        return 0, 0
    try:
        return _cached_report_counts(report_path, mtime_ns)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0, 0


@lru_cache(maxsize=128)
def _cached_report_entries(path: str, mtime_ns: int) -> tuple:
    """((error_type, error_label), ...) per report line, in line order —
    the metadata behind the issue-type filter (issue ids are
    ``{table_type}-{line index}``; see oc_validator's map_errors_to_data)."""
    entries = load_jsonl_report(path)
    return tuple((e.get('error_type', ''), e.get('error_label', ''))
                 for e in entries)


def _report_entries(report_path: Optional[str]) -> tuple:
    """Per-line (error_type, error_label) tuples; () when missing/unreadable.
    Same mtime-keyed caching + guard pattern as ``_report_counts``."""
    if not report_path:
        return ()
    try:
        mtime_ns = Path(report_path).stat().st_mtime_ns
    except OSError:
        return ()
    try:
        return _cached_report_entries(report_path, mtime_ns)
    except (OSError, ValueError, json.JSONDecodeError):
        return ()


def _parse_csv_param(raw: Optional[str]) -> set:
    """Comma-separated query param → set of stripped non-empty items."""
    return {p.strip() for p in (raw or '').split(',') if p.strip()}


def _csv_data_rows(csv_path: Optional[str]) -> int:
    """Data-row count of a session CSV (0 when unreadable) — used by the
    table-less legacy branches, which have no artifacts to count rows from."""
    if not csv_path:
        return 0
    try:
        return sum(1 for _ in CSVStreamReader(csv_path))
    except Exception:
        return 0


def _table_header_html(label: str, errors: int, warnings: int,
                       invalid_rows: int, total_rows: int,
                       filename: Optional[str] = None,
                       table_type: Optional[str] = None) -> str:
    """Compact per-table header (table-type title + one-line validation
    stats) that replaces the verbose general-info block emitted by
    ``make_gui``.  The ``table-stats`` class marks the new format;
    ``data-table-type`` (when given) is the per-table anchor used by the
    frontend for scoped queries and scroll targeting."""
    attrs = f' data-table-type="{table_type}"' if table_type else ''
    parts = [
        f'<div class="container-fluid general-info table-stats"{attrs}>',
        f'<h4>{label}</h4>',
        f'<p class="table-stats-line">Issues: {errors + warnings} (errors: {errors}; warnings: {warnings})'
        f' | Invalid rows: {invalid_rows} | Total rows: {total_rows}</p>',
    ]
    if filename is not None:
        parts.append(f'<p class="text-success mb-0"><strong>✓ No issues found '
                     f'in <em>{filename}</em>.</strong></p>')
    parts.append('</div>')
    return ''.join(parts)


def _table_tools_html(table_type: str, q: Optional[str],
                      match_count: int, facet_dropdown: str = '') -> str:
    """Per-table tools row: the full-table search box + the issue-filter
    dropdown.  Server-rendered so the input's value is state-correct across
    re-renders; typing is debounced in editor.js
    (``[data-search-input]``), which also restores focus/caret after each
    re-render.  ``type="search"`` gives the native clear (×) affordance."""
    value = html.escape(q, quote=True) if q else ''
    count = ''
    if q:
        rows_label = 'row' if match_count == 1 else 'rows'
        count = (f'<span class="search-count">{match_count} {rows_label} '
                 f'matching “{html.escape(q, quote=True)}”</span>')
    return (f'<div class="table-tools" data-table-type="{table_type}">'
            f'<input type="search" class="form-control form-control-sm '
            f'table-search" data-search-input="{table_type}" '
            f'value="{value}" placeholder="Search this table…" '
            f'aria-label="Search this table"> {count}{facet_dropdown}</div>')


def _facet_desc(types: set) -> str:
    """Human summary of the selected issue types for the filter banner."""
    return ', '.join(sorted(t.replace('_', ' ') for t in types))


def _facet_dropdown_html(table_type: str, facet_rows: dict, types: set) -> str:
    """Issue-filter dropdown for one table (server-rendered, state-correct:
    checkboxes reflect the current selection).  ``facet_rows`` maps
    ``(severity, label)`` → live row ids; counts are rows affected.  The
    severity group checkbox is a client-only select-all for its labels;
    Apply/Clear are delegated in editor.js (``[data-facet-apply]`` /
    ``[data-facet-clear]``).  ``data-bs-auto-close="outside"`` keeps the
    menu open on checkbox clicks."""
    if not facet_rows:
        return ''
    groups = []
    for sev, title in (('error', 'Errors'), ('warning', 'Warnings')):
        items = sorted((label, rows) for (s, label), rows in facet_rows.items()
                       if s == sev)
        if not items:
            continue
        group_rows = set().union(*(rows for _, rows in items))
        group_checked = all(label in types for label, _ in items)
        items_html = []
        for label, rows in items:
            n = len(rows)
            rows_label = 'row' if n == 1 else 'rows'
            items_html.append(
                f'<label class="facet-item">'
                f'<input type="checkbox" data-label="{html.escape(label, quote=True)}"'
                f'{" checked" if label in types else ""}>'
                f'<span>{html.escape(label.replace("_", " "))}</span>'
                f'<span class="facet-count">{n} {rows_label}</span>'
                f'</label>')
        groups.append(
            f'<div class="facet-group">'
            f'<div class="facet-group-head">'
            f'<input type="checkbox" data-facet-group="{sev}"'
            f'{" checked" if group_checked else ""} '
            f'aria-label="Select all {title.lower()}">'
            f'<strong>{title}</strong>'
            f'<span class="facet-meta">{len(items)} types · {len(group_rows)} rows</span>'
            f'</div>{"".join(items_html)}</div>')
    return (f'<div class="btn-group facet-dropdown ms-auto">'
            f'<button type="button" class="btn btn-sm btn-outline-secondary '
            f'view-toggle" data-bs-toggle="dropdown" '
            f'data-bs-auto-close="outside" aria-expanded="false" '
            f'title="Filter rows by issue type">Issue filters ▾</button>'
            f'<div class="dropdown-menu dropdown-menu-end p-2 facet-menu" '
            f'data-table-type="{table_type}">{"".join(groups)}'
            f'<div class="d-flex gap-2 mt-2">'
            f'<button type="button" class="btn btn-sm btn-primary" '
            f'data-facet-apply="{table_type}">Apply</button>'
            f'<button type="button" class="btn btn-sm btn-outline-secondary" '
            f'data-facet-clear="{table_type}">Clear</button>'
            f'</div></div></div>')


def _table_controls_html(table_type: str, show_all: bool, changes_only: bool,
                         filtered: bool) -> str:
    """Per-table filter buttons, rendered with each table's fragment (each
    table of a paired session filters independently).  ``filtered`` (the
    issue-filtered view) disables both toggles.  Clicks are delegated in
    editor.js via ``[data-table-toggle]``."""
    def btn(kind: str, label: str, active: bool) -> str:
        return (f'<button type="button" class="btn btn-sm btn-outline-secondary '
                f'view-toggle{" active" if active else ""}" '
                f'data-table-toggle="{kind}" data-table-type="{table_type}"'
                f'{" disabled" if filtered else ""}>{label}</button>')
    return (f'<div class="table-controls" data-table-type="{table_type}">'
            + btn('show-all', 'Show all rows', show_all)
            + btn('changes-only', 'Show Changes Only', changes_only)
            + '</div>')


def _filter_banner_html(table_type: str, row_count: int,
                        issue_id: Optional[str] = None,
                        facet_desc: Optional[str] = None) -> str:
    """Banner above a table in a filtered view — the single-issue view
    (``issue_id``) or the issue-type facet selection (``facet_desc``).
    Server-rendered; the exit button is delegated in editor.js via
    ``[data-exit-filter]``."""
    rows_label = 'row' if row_count == 1 else 'rows'
    if issue_id is not None:
        what = f'Filtered by issue: <strong>{html.escape(issue_id)}</strong>'
    else:
        what = (f'Filtered by issues: '
                f'<strong>{html.escape(facet_desc or "selected types")}</strong>')
    return (f'<div class="filter-banner" data-table-type="{table_type}">'
            f'<div class="filter-banner-content">'
            f'<button type="button" class="btn btn-sm btn-outline-primary" '
            f'data-exit-filter="{table_type}">← Back to full table</button>'
            f'<span class="filter-banner-text">{what}</span>'
            f'<span class="badge bg-secondary">{row_count} {rows_label}</span>'
            f'</div></div>')


def _empty_state_html(table_type: str, show_all: bool, changes_only: bool,
                      filtered: bool, q: Optional[str] = None) -> str:
    """Context-dependent message when a table's filtered view has no rows
    (server-rendered; the CTA button is delegated via
    ``[data-empty-show-all]``)."""
    if q and filtered:
        text = f'No rows matching “{html.escape(q)}” within the filtered view.'
    elif q:
        text = f'No rows matching “{html.escape(q)}”.'
    elif filtered:
        text = ('No rows match the active issue filters '
                '(deleted rows drop out of the filtered view).')
    elif changes_only:
        text = ('No changes yet — edit, add or delete content, or turn off '
                '“Show Changes Only”.')
    elif not show_all:
        return (f'<div class="empty-state" data-table-type="{table_type}">'
                f'No rows with errors/warnings or changes.'
                f'<button type="button" class="btn btn-sm btn-outline-primary '
                f'ms-2" data-empty-show-all="{table_type}">Show all rows'
                f'</button></div>')
    else:
        text = 'The table is empty.'
    return (f'<div class="empty-state" data-table-type="{table_type}">'
            f'{text}</div>')


async def _table_fragment(session_id: str, table_type: str, *, page: int,
                          show_all: bool, changes_only: bool,
                          issue_id: Optional[str],
                          focus_row_id: Optional[str],
                          report_path: Optional[str],
                          csv_filename: Optional[str] = None,
                          q: Optional[str] = None,
                          issue_types: Optional[str] = None):
    """Build one table's page fragment: stats header (+ per-table filter
    controls, filter banner, empty state) + journal-replayed table page +
    pager.  Returns ``(html, info)`` where ``info`` carries the clamped
    page/page_count and the filtered-view row count, or ``(None, None)``
    when the table has no parsable table (fully-valid side).
    ``csv_filename`` adds the ✓ "no issues" line under the header when the
    table's report is empty.  Caller must hold the session lock.
    """
    loaded = await load_journal_view(session_id, table_type)
    if loaded is None or not loaded[2]['artifacts'].get('has_table'):
        return None, None
    _journal, view, state = loaded
    artifacts = state['artifacts']
    label = 'Metadata' if table_type == 'meta' else 'Citations'

    deletions = view.compute_deletions()
    changed = view.changed_row_ids(deletions)
    issues = view.issue_row_ids()

    # Issue-type filter (faceted view): joins the artifacts' issue_index
    # (issue id → rows) with the report's per-line (severity, label)
    # metadata.  Ids are renumbered on every revalidate, so the selection
    # travels as error *labels*, never ids.
    types = _parse_csv_param(issue_types)
    facet_meta = _report_entries(report_path)
    live = set(view.row_ids)

    def _facet_line(iid: str) -> Optional[tuple]:
        """(severity, label) of the report line behind an issue id, if any."""
        prefix = f'{table_type}-'
        if not iid.startswith(prefix):
            return None
        try:
            idx = int(iid[len(prefix):])
        except ValueError:
            return None
        return facet_meta[idx] if 0 <= idx < len(facet_meta) else None

    facet_active = bool(types)
    facet_rows: dict = {}   # (severity, label) → live row ids (dropdown counts)
    keep: set = set()
    for iid, row_ids in artifacts['issue_index'].items():
        line = _facet_line(iid)
        if line is None:
            continue
        rows_here = {r for r in row_ids if r in live}
        if not rows_here:
            continue
        facet_rows.setdefault(line, set()).update(rows_here)
        if facet_active and line[1] in types:
            keep.update(rows_here)

    # Full-table search: rows whose current values contain q.  A query
    # implies show-all-like base visibility, but still ANDs with
    # changes_only and with the issue-filter predicates.
    search_ids = view.search_row_ids(q) if q else None

    def _matches_search(rid: str) -> bool:
        return search_ids is None or rid in search_ids

    if issue_id is not None:
        # dict.fromkeys dedupes while keeping document order: an issue can
        # involve several cells of the SAME row (self-citation), and
        # artifacts built before the issue_index dedupe fix list that row
        # once per icon.
        entries = [{'row_id': rid, 'ghost': False}
                   for rid in dict.fromkeys(
                       artifacts['issue_index'].get(issue_id, []))
                   if rid in view.row_ids and _matches_search(rid)]
    elif facet_active:
        # Facet-filtered view: live rows carrying at least one issue of a
        # selected type (ghosts excluded, like the single-issue view).
        entries = [e for e in view.display_rows()
                   if e['row_id'] in keep and _matches_search(e['row_id'])]
    else:
        entries = [e for e in view.display_rows()
                   if (search_ids is not None
                       or show_all or e['row_id'] in issues
                       or e['row_id'] in changed)
                   and (not changes_only or e['row_id'] in changed)
                   and _matches_search(e['row_id'])]

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

    open_tag = artifacts['table_open_tag'].replace(
        '>', f' data-editable="1" data-table-type="{table_type}">', 1)
    # Whole-table stats (the pager's `total` is the filtered-view count):
    # errors/warnings from the last validation report; invalid/total rows
    # from the live view (ghosts excluded, journal-added rows included).
    errors, warnings = _report_counts(report_path)
    live_rows = set(view.row_ids)
    invalid_rows = len(issues & live_rows)
    total_rows = len(view.row_ids)

    filtered = issue_id is not None or facet_active
    # A table whose report is empty (fully valid) keeps the ✓ reassurance.
    header_filename = csv_filename if errors + warnings == 0 else None
    parts = [_table_header_html(label, errors, warnings, invalid_rows,
                                total_rows, filename=header_filename,
                                table_type=table_type)]
    if filtered:
        if issue_id is not None:
            parts.append(_filter_banner_html(table_type, total,
                                             issue_id=issue_id))
        else:
            parts.append(_filter_banner_html(
                table_type, total, facet_desc=_facet_desc(types)))
    parts.append(_table_tools_html(
        table_type, q, total,
        facet_dropdown=_facet_dropdown_html(table_type, facet_rows, types)))
    if not entries:
        parts.append(_empty_state_html(table_type, show_all, changes_only,
                                       filtered, q=q))
    parts.extend([
        _table_controls_html(table_type, show_all, changes_only, filtered),
        '<div class="table-container container-fluid">',
        open_tag, artifacts['thead_html'], '<tbody>',
        *row_parts,
        '</tbody></table></div>',
        _pager_html(table_type, page, page_count, total),
    ])
    info = {"page": page}
    return ''.join(parts), info


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EditItemRequest(BaseModel):
    session_id: str
    item_id: str
    new_value: str
    table_type: Optional[str] = None   # 'meta' or 'cits'; None = primary


class DeleteItemRequest(BaseModel):
    session_id: str
    item_id: str
    table_type: Optional[str] = None


class AddItemRequest(BaseModel):
    session_id: str
    row_id: str        # e.g. "row5"
    field_name: str    # e.g. "id", "author"
    new_value: str     # value for the new item (non-empty; validated client-side)
    table_type: Optional[str] = None


class RevalidateRequest(BaseModel):
    session_id: str
    verify_id_existence: Optional[bool] = None


class DeleteRowRequest(BaseModel):
    session_id: str
    row_id: str   # e.g. "row5"
    table_type: Optional[str] = None


class AddRowRequest(BaseModel):
    session_id: str
    table_type: Optional[str] = None


class ClearCellRequest(BaseModel):
    session_id: str
    row_id: str       # e.g. "row5"
    field_name: str   # e.g. "id", "author"
    table_type: Optional[str] = None


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
                         focus_row_id: Optional[str] = None, cits_page: int = 1,
                         cits_show_all: bool = False,
                         cits_changes_only: bool = False,
                         cits_issue_id: Optional[str] = None,
                         cits_focus_row_id: Optional[str] = None,
                         q: Optional[str] = None,
                         cits_q: Optional[str] = None,
                         issue_types: Optional[str] = None,
                         cits_issue_types: Optional[str] = None):
    """
    One page (≤ TABLE_PAGE_SIZE rows) per table of the editor's view —
    every table of the session (metadata and citations, both editable) is
    journal-replayed and paginated/filtered independently.

    Row visibility (per table): by default only rows with issues
    (errors/warnings) or with journal changes (edits / additions /
    deletions — ghost rows count as changed); ``show_all`` reveals every
    row regardless of validity, ``changes_only`` restricts the view to
    changed rows.  ``issue_id`` switches to the single-issue filtered
    view.  Ghost overlays for deleted items/rows are always part of the
    rendered rows.  Rows are selected by id, never by position;
    ``focus_row_id`` (a live row id, or a ``ghost-`` prefixed one)
    selects the page containing that row.

    The top-level params describe the session's *primary* table (the
    first of meta/cits with a parsable table); in paired sessions the
    ``cits_*`` params describe the citations table when it is not the
    primary one.
    """
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(session_id):
        states = {}
        for tt in session_table_types(session):
            states[tt] = await load_table_state(session_id, tt)
        primary = next((tt for tt in ('meta', 'cits')
                        if tt in states and states[tt] is not None
                        and states[tt]['artifacts'].get('has_table')), None)

        if primary is None:
            # No parsable table anywhere (corrupt/missing baselines).
            # Serve the zero-stats header cards only — nothing to edit.
            cards = []
            for t in session_table_types(session):
                csv_path = (session.meta_csv_path if t == 'meta'
                            else session.cits_csv_path) or ''
                cards.append(_table_header_html(
                    'Metadata' if t == 'meta' else 'Citations', 0, 0, 0,
                    _csv_data_rows(csv_path),
                    filename=Path(csv_path).name if csv_path else None,
                    table_type=t))
            return {"html": ''.join(cards), "page": 1,
                    "table_type": editable_table_type(session), "cits": None}

        # The primary table's fragment is described by the top-level params
        # (compat); the other table — only possible when primary is 'meta'
        # — by the cits_* params.
        q = (q or '').strip() or None
        cits_q = (cits_q or '').strip() or None
        params_by_table = {primary: dict(page=page, show_all=show_all,
                                         changes_only=changes_only,
                                         issue_id=issue_id,
                                         focus_row_id=focus_row_id,
                                         q=q, issue_types=issue_types)}
        other = 'cits' if primary == 'meta' else 'meta'
        if other in states and states[other] is not None \
                and states[other]['artifacts'].get('has_table'):
            params_by_table[other] = dict(page=cits_page, show_all=cits_show_all,
                                          changes_only=cits_changes_only,
                                          issue_id=cits_issue_id,
                                          focus_row_id=cits_focus_row_id,
                                          q=cits_q,
                                          issue_types=cits_issue_types)

        html_parts: list = []
        info_by_table: dict = {}
        for tt in session_table_types(session):   # document order: meta, cits
            if tt not in params_by_table:
                continue
            report_path = (session.meta_report_path if tt == 'meta'
                           else session.cits_report_path)
            csv_path = (session.meta_csv_path if tt == 'meta'
                        else session.cits_csv_path) or ''
            html, info = await _table_fragment(session_id, tt,
                                               report_path=report_path,
                                               csv_filename=Path(csv_path).name
                                               if csv_path else None,
                                               **params_by_table[tt])
            html_parts.append(html)
            info_by_table[tt] = info

        # Fully-valid (table-less) sides still get their zero-stats card,
        # in document order, so the "✓ no issues" reassurance is kept.
        # The response's `cits` key describes the citations table only when
        # it is NOT the primary (in solo-cits sessions the top-level fields
        # already describe it).
        cits_info = info_by_table.get('cits') if primary != 'cits' else None
        for tt in session_table_types(session):
            if tt in info_by_table or tt not in states:
                continue
            csv_path = (session.meta_csv_path if tt == 'meta'
                        else session.cits_csv_path) or ''
            card = _table_header_html('Metadata' if tt == 'meta' else 'Citations',
                                      0, 0, 0, _csv_data_rows(csv_path),
                                      filename=Path(csv_path).name
                                      if csv_path else None,
                                      table_type=tt)
            if tt == 'meta':
                html_parts.insert(0, card)
            else:
                html_parts.append(card)
                cits_info = {"page": 1}

        primary_info = info_by_table[primary]
    return {"html": ''.join(html_parts),
            "page": primary_info["page"],
            "table_type": primary, "cits": cits_info}


@router.post("/item")
async def edit_item(request: EditItemRequest):
    """
    Edit a single item — appends a ``set_item`` (or ``remove_item`` when a
    multi-value item is emptied, mirroring the auto-remove behaviour) event.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = resolve_table_type(session, request.table_type)
    row_id = _row_id_for_item(request.item_id)
    field_name = _field_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        journal, view, _state = await _load_view(request.session_id, session,
                                                 table_type)
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
            await _append_event(session, request.session_id, table_type,
                                journal, 'remove_item', row=row_id,
                                item=request.item_id, field=field_name)
        else:
            await _append_event(session, request.session_id, table_type,
                                journal, 'set_item', row=row_id,
                                item=request.item_id, field=field_name,
                                value=request.new_value)

        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": row_id,
        "table_type": table_type
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

    table_type = resolve_table_type(session, request.table_type)

    field_name = request.field_name
    is_multi_value = field_name in HTMLParser.ITEM_SEPARATORS

    async with document_cache.session_lock(request.session_id):
        journal, view, _state = await _load_view(request.session_id, session,
                                                 table_type)
        _bs, row = view.row_soup(request.row_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Row '{request.row_id}' not found")

        has_value, _ = HTMLParser.get_cell_state_in_row(row, field_name)

        if not has_value:
            await _append_event(session, request.session_id, table_type,
                                journal, 'init_cell', row=request.row_id,
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
            await _append_event(session, request.session_id, table_type,
                                journal, 'append_item', row=request.row_id,
                                field=field_name, value=request.new_value)

        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": request.row_id,
        "table_type": table_type
    }


@router.delete("/item")
async def delete_item(request: DeleteItemRequest):
    """Delete a specific item from a multi-value cell (``remove_item``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = resolve_table_type(session, request.table_type)
    row_id = _row_id_for_item(request.item_id)
    field_name = _field_for_item(request.item_id)

    async with document_cache.session_lock(request.session_id):
        journal, view, _state = await _load_view(request.session_id, session,
                                                 table_type)
        bs, row = view.row_soup(row_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Item '{request.item_id}' not found")
        if HTMLParser.get_item_value_from_soup(bs, request.item_id) is None:
            # Already absent — success no-op (matches the historical
            # lenient behaviour of remove_item).
            return {
                "success": True,
                "row_id": row_id,
                "table_type": table_type
            }

        await _append_event(session, request.session_id, table_type,
                            journal, 'remove_item', row=row_id,
                            item=request.item_id, field=field_name)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": row_id,
        "table_type": table_type
    }


@router.post("/row/delete")
async def delete_row(request: DeleteRowRequest):
    """Delete an entire table row (``delete_row``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = resolve_table_type(session, request.table_type)
    async with document_cache.session_lock(request.session_id):
        journal, view, _state = await _load_view(request.session_id, session,
                                                 table_type)
        if request.row_id not in view.row_ids:
            # Already gone — success no-op.
            return {"success": True, "row_id": request.row_id,
                    "table_type": table_type}

        await _append_event(session, request.session_id, table_type,
                            journal, 'delete_row', row=request.row_id)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "row_id": request.row_id,
            "table_type": table_type}


@router.post("/row/add")
async def add_row(request: AddRowRequest):
    """Add a new empty row at the end of the table (``add_row``)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = resolve_table_type(session, request.table_type)
    async with document_cache.session_lock(request.session_id):
        journal, view, state = await _load_view(request.session_id, session,
                                                table_type)
        if not state['artifacts'].get('has_table'):
            raise HTTPException(status_code=500, detail="Failed to add new row")
        # Reserved = every row id this table's journal has EVER recorded
        # (applied events + redo tail): the view is built from applied
        # events only, so an added-then-deleted or undone id would
        # otherwise look free and be re-issued — replay would splice the
        # old incarnation's item events onto the new row.
        new_row_id = view.next_add_row_id(
            reserved_rows={ev['row'] for ev in journal.events})
        await _append_event(session, request.session_id, table_type,
                            journal, 'add_row', row=new_row_id)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": new_row_id,
        "table_type": table_type
    }


@router.post("/cell/clear")
async def clear_cell_route(request: ClearCellRequest):
    """Clear all values from a cell, leaving one empty item-container."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    table_type = resolve_table_type(session, request.table_type)
    async with document_cache.session_lock(request.session_id):
        journal, view, _state = await _load_view(request.session_id, session,
                                                 table_type)
        bs, row = view.row_soup(request.row_id)
        if row is None or HTMLParser.get_cell_in_row(row, request.field_name) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Cell '{request.field_name}' not found in row '{request.row_id}'"
            )

        await _append_event(session, request.session_id, table_type,
                            journal, 'clear_cell', row=request.row_id,
                            field=request.field_name)
        session.mark_edited()
        await SessionManager.save_session(session)

    return {
        "success": True,
        "row_id": request.row_id,
        "table_type": table_type
    }


# ---------------------------------------------------------------------------
# Undo / Redo
# ---------------------------------------------------------------------------

@router.get("/undo_state/{session_id}")
async def get_undo_state(session_id: str):
    """Undo/redo availability for the session-wide stack (any journal)."""
    session = await SessionManager.load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    async with document_cache.session_lock(session_id):
        tables = {}
        for tt in session_table_types(session):
            journal, _view, _state = await _load_view(session_id, session, tt)
            tables[tt] = {"can_undo": journal.can_undo,
                          "can_redo": journal.can_redo}
    return {"tables": tables,
            "can_undo": any(t["can_undo"] for t in tables.values()),
            "can_redo": any(t["can_redo"] for t in tables.values())}


@router.post("/undo")
async def undo(request: UndoRedoRequest):
    """Undo the most recent mutation across BOTH tables: move the cursor of
    whichever journal holds the chronologically last applied event (ordered
    by each event's ``gseq`` stamp)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journals = {}
        for tt in session_table_types(session):
            journals[tt] = await _load_view(request.session_id, session, tt)

        target_tt = None
        target_key = None
        for tt, (journal, _view, _state) in journals.items():
            applied = journal.applied_events
            if not applied:
                continue
            key = (applied[-1]['gseq'], tt)
            if target_key is None or key > target_key:
                target_tt, target_key = tt, key

        if target_tt is None:
            return {"success": False, "message": "Nothing to undo"}

        journal, _view, _state = journals[target_tt]
        ev = await journal.undo()
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "table_type": target_tt, "row_id": ev['row']}


@router.post("/redo")
async def redo(request: UndoRedoRequest):
    """Redo the oldest undone mutation across BOTH tables: move the cursor
    of whichever journal holds the chronologically first event in a redo
    tail (ordered by each event's ``gseq`` stamp)."""
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        journals = {}
        for tt in session_table_types(session):
            journals[tt] = await _load_view(request.session_id, session, tt)

        target_tt = None
        target_key = None
        for tt, (journal, _view, _state) in journals.items():
            tail = journal.events[journal.cursor:]
            if not tail:
                continue
            key = (tail[0]['gseq'], tt)
            if target_key is None or key < target_key:
                target_tt, target_key = tt, key

        if target_tt is None:
            return {"success": False, "message": "Nothing to redo"}

        journal, _view, _state = journals[target_tt]
        ev = await journal.redo()
        session.mark_edited()
        await SessionManager.save_session(session)

    return {"success": True, "table_type": target_tt, "row_id": ev['row']}


# ---------------------------------------------------------------------------
# Commit (Save)
# ---------------------------------------------------------------------------

@router.post("/commit")
async def commit(request: CommitRequest):
    """
    Save: materialize the current view (baseline + events ≤ cursor) of every
    uploaded table into its table file.  Journals and undo history are
    preserved — undo still steps back past the save.
    """
    session = await SessionManager.load_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async with document_cache.session_lock(request.session_id):
        for tt in session_table_types(session):
            journal, view, _state = await _load_view(request.session_id,
                                                     session, tt)
            await SessionManager.save_html(request.session_id, view.html, tt)
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
                    request.session_id, session, 'meta')
                meta_rows = (await asyncio.to_thread(meta_view.rows_for_export)
                             if meta_state['artifacts'].get('has_table') else None)

                cits_journal, cits_view, cits_state = await _load_view(
                    request.session_id, session, 'cits')
                cits_rows = (await asyncio.to_thread(cits_view.rows_for_export)
                             if cits_state['artifacts'].get('has_table') else None)

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

                # Regenerate each table via a temp file, read it back once,
                # then atomically into its commit artifact + baseline.
                temp_meta_html = session_dir / 'temp_meta_revalidate.html'
                temp_cits_html = session_dir / 'temp_cits_revalidate.html'
                await asyncio.to_thread(ValidatorService.generate_table_html,
                                        str(temp_meta_csv), meta_report_path,
                                        str(temp_meta_html), meta_is_valid,
                                        'Metadata')
                await asyncio.to_thread(ValidatorService.generate_table_html,
                                        str(temp_cits_csv), cits_report_path,
                                        str(temp_cits_html), cits_is_valid,
                                        'Citations')
                with open(temp_meta_html, 'r', encoding='utf-8', newline='') as f:
                    new_meta_html = f.read()
                with open(temp_cits_html, 'r', encoding='utf-8', newline='') as f:
                    new_cits_html = f.read()
                temp_meta_html.unlink(missing_ok=True)
                temp_cits_html.unlink(missing_ok=True)

                await SessionManager.save_html(request.session_id, new_meta_html, 'meta')
                await SessionManager.save_html(request.session_id, new_cits_html, 'cits')
                await SessionManager.save_baseline_snapshot(
                    request.session_id, new_meta_html, 'meta')
                await SessionManager.save_baseline_snapshot(
                    request.session_id, new_cits_html, 'cits')
                gen_meta = await build_generation_artifacts(request.session_id, 'meta')
                gen_cits = await build_generation_artifacts(request.session_id, 'cits')
                await journal.reset(gen_meta, 'meta')
                await cits_journal.reset(gen_cits, 'cits')

                session.meta_report_path = meta_report_path
                session.cits_report_path = cits_report_path
                total_error_count = (
                    len(await asyncio.to_thread(load_jsonl_report, meta_report_path))
                    + len(await asyncio.to_thread(load_jsonl_report,
                                                  cits_report_path)))

                if temp_meta_csv.name.startswith('temp_'):
                    temp_meta_csv.unlink(missing_ok=True)
                if temp_cits_csv.name.startswith('temp_'):
                    temp_cits_csv.unlink(missing_ok=True)

            else:
                # ── Single-table re-validation ──────────────────────────────
                table_type = editable_table_type(session)
                journal, view, state = await _load_view(request.session_id, session)
                rows_data = (await asyncio.to_thread(view.rows_for_export)
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
                    verify_id_existence=verify_id,
                    expected_type='meta_csv' if table_type == 'meta' else 'cits_csv'
                )

                temp_html_path = session_dir / 'temp_revalidate.html'
                await asyncio.to_thread(
                    ValidatorService.generate_table_html, str(temp_csv_path),
                    report_path, str(temp_html_path), is_valid,
                    'Metadata' if table_type == 'meta' else 'Citations')
                with open(temp_html_path, 'r', encoding='utf-8', newline='') as f:
                    new_html = f.read()
                temp_html_path.unlink(missing_ok=True)

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
                total_error_count = len(
                    await asyncio.to_thread(load_jsonl_report, report_path))

                if temp_csv_path.name.startswith('temp_'):
                    temp_csv_path.unlink(missing_ok=True)

            session.mark_validated()
            session.verify_id_existence = verify_id
            await SessionManager.save_session(session)

            return {
                "success": True,
                "error_count": total_error_count
            }

        except HTTPException:
            raise
        except TableNotMatchingInstance as e:
            # Defensive: revalidate data comes from the session's own table,
            # so a type mismatch should never happen — surface it as 400.
            raise HTTPException(status_code=400, detail=str(e))
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
        edited_count = 0
        unsaved = False
        can_undo = False
        can_redo = False
        for tt in session_table_types(session):
            journal, view, _state = await _load_view(session_id, session, tt)
            edited_count += len(view.edited_item_ids)
            unsaved = unsaved or journal.has_unsaved_changes
            can_undo = can_undo or journal.can_undo
            can_redo = can_redo or journal.can_redo

    return {
        "session_id": session.session_id,
        "has_metadata": session.has_metadata,
        "has_citations": session.has_citations,
        "verify_id_existence": session.verify_id_existence,
        "draft_name": session.draft_name,
        "has_edits_since_validation": session.has_edits_since_validation,
        "edited_items_count": edited_count,
        "unsaved_changes": unsaved,
        "can_undo": can_undo,
        "can_redo": can_redo,
        "last_validated_at": session.last_validated_at
    }
