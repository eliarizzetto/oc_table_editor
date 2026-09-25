/* OC Table Editor - JavaScript Utilities */

// ── Bootstrap widget initialisation ──────────────────────────────────────────
// Exposed as a named function so it can be re-called after dynamic HTML injection
// (the table HTML is loaded asynchronously, so DOMContentLoaded is too early).
//
// Popovers/tooltips are initialised lazily via delegation (see
// setupTableDelegation) — initialising ~1300 issue icons eagerly after every
// table load was a major cost on large tables. This function only needs to
// keep the oc_validator overrides in place.

function initBootstrapWidgets() {
    // Override the highlightInvolvedElements function that oc_validator embeds in the HTML.
    // The embedded script runs after HTML injection and defines its own version,
    // so we must override it here to redirect to our filtered-view behavior.
    window.highlightInvolvedElements = function(clickedIssue) {
        // Hide the popover immediately so it doesn't cover the filtered view
        const popover = bootstrap.Popover.getInstance(clickedIssue);
        if (popover) popover.hide();

        const table = clickedIssue.closest('table[data-table-type]');
        if (!table || !table.hasAttribute('data-editable')) return;  // read-only tables
        const issueId = clickedIssue.id;
        if (!issueId) {
            console.warn('Issue icon has no id attribute');
            return;
        }
        loadFilteredTable(issueId, table.dataset.tableType);
    };

    // Also override clearHighlights to do nothing (we don't use it anymore)
    window.clearHighlights = function() {};
}

// ── View state + rendering core ──────────────────────────────────────────────
// Every view is one page (≤ 25 rows) per table, fetched from
// GET /api/edit/table/{session_id}.  Both tables of a paired session are
// editable and paginated/filtered/undone independently: each has its own
// state object here.  `primaryTable` is the table the server describes with
// the top-level params (the other one uses the cits_* params); `activeTable`
// is the last table the user interacted with — undo/redo (sidebar buttons
// and Ctrl+Z/Y) target it.

function mkTableState() {
    return { page: 1, showAllRows: false, changesOnly: false, q: '' };
}
const tableStates = { meta: mkTableState(), cits: mkTableState() };
const filteredStates = { meta: null, cits: null };  // {issueId, page, types} per table
const savedStates = { meta: null, cits: null };     // captured on entering the filtered view
let primaryTable = 'meta';   // corrected from the response's table_type on first render
let activeTable = null;      // last-interacted table (undo/redo target)
let renderSeq = 0;           // stale-response guard for overlapping fetches

/** Query-param name for a per-table option: the primary table uses the
 *  historical top-level names, the other table the cits_* ones. */
function paramName(tt, base) {
    return tt === primaryTable ? base : 'cits_' + base;
}

/** Track the last-interacted table (used as the fallback target for
 *  filtered-view entry, not for undo/redo — that stack is session-wide). */
function setActiveTable(tt) {
    if (!tt) return;
    activeTable = tt;
}

/** Scroll so the given table's stats header sits at the viewport top —
 *  paging/filtering never yanks the view away from the table acted upon. */
function scrollToTableHeader(tt, container) {
    const el = container || document.getElementById('tableContainer');
    if (!el) return;
    const anchor = el.querySelector(`.table-stats[data-table-type="${tt}"]`);
    if (anchor) anchor.scrollIntoView({ block: 'start' });
    else el.scrollIntoView({ block: 'start' });
}

// Cell display mode: 'reduced' (default) collapses fully-valid cells to one
// truncated line and hides valid items in error cells behind an ellipsis;
// 'expanded' shows full content.  Purely client-side and **per table** (each
// table's "Expand cells" button works independently); never captured by the
// filtered-view snapshots.  Row overrides survive re-renders and pagination,
// keyed 'meta:rowN' / 'cits:rowN' (the two tables of a paired session reuse
// the same row ids).
const cellDisplay = {
    modes: { meta: 'reduced', cits: 'reduced' },
    rowOverrides: new Map(),
};

/**
 * Fetch and inject the current view (both tables of a paired session).
 * opts: { preserveScroll, scrollToTable, focusRowId, focusTable }
 */
async function renderView(opts = {}) {
    const container = document.getElementById('tableContainer');
    const sid = window.currentSessionId;
    if (!container || !sid) return;

    // Until the first response arrives we don't know which table is the
    // primary one — all state is at its defaults then, so the initial
    // param naming assumption ('meta' primary) is harmless.
    const params = new URLSearchParams();
    for (const tt of ['meta', 'cits']) {
        const filtered = filteredStates[tt];
        const st = filtered || tableStates[tt];
        params.set(paramName(tt, 'page'), String(st.page));
        if (tableStates[tt].q) params.set(paramName(tt, 'q'), tableStates[tt].q);
        if (filtered) {
            if (filtered.issueId) params.set(paramName(tt, 'issue_id'), filtered.issueId);
            if (filtered.types && filtered.types.length)
                params.set(paramName(tt, 'issue_types'), filtered.types.join(','));
        } else {
            params.set(paramName(tt, 'show_all'), String(st.showAllRows));
            params.set(paramName(tt, 'changes_only'), String(st.changesOnly));
        }
    }
    if (opts.focusRowId && opts.focusTable) {
        params.set(paramName(opts.focusTable, 'focus_row_id'), opts.focusRowId);
    }

    const seq = ++renderSeq;
    const scrollY = window.scrollY;
    try {
        const response = await fetch(`/api/edit/table/${sid}?${params.toString()}`);
        const data = await response.json();
        if (seq !== renderSeq) return;  // a newer request superseded this one
        if (!response.ok) throw new Error(data.detail || 'Failed to load table view');

        if (data.table_type) primaryTable = data.table_type;
        if (!activeTable) activeTable = primaryTable;

        // Server-side page clamping is authoritative, per table (the
        // primary's info is top-level; the other table's under data.cits).
        const infoFor = tt => (tt === primaryTable)
            ? { page: data.page }
            : (data.cits || null);
        for (const tt of ['meta', 'cits']) {
            const info = infoFor(tt);
            if (info) (filteredStates[tt] || tableStates[tt]).page = info.page;
        }

        // Capture the focused search box (if any) so the innerHTML swap
        // below doesn't kill it mid-keystroke — the debounced search
        // re-renders on every typing pause.
        const activeEl = document.activeElement;
        let searchFocus = null;
        if (activeEl && activeEl.matches &&
                activeEl.matches('[data-search-input]')) {
            searchFocus = { tt: activeEl.dataset.searchInput,
                            start: activeEl.selectionStart,
                            end: activeEl.selectionEnd };
        }
        // Empty states, filter banners and the per-table filter buttons are
        // server-rendered with each fragment; the per-table "Expand cells"
        // buttons are client-injected into the same controls row.
        if (typeof disposePopoversIn === 'function') disposePopoversIn(container);
        container.innerHTML = data.html;
        if (typeof setupEditHandlers === 'function') setupEditHandlers();
        applyCellDisplay();   // after enhanceRow (buttons exist) and before the focus scroll
        initBootstrapWidgets();
        if (searchFocus) {
            const el = container.querySelector(
                `[data-search-input="${searchFocus.tt}"]`);
            if (el) {
                el.focus();
                try { el.setSelectionRange(searchFocus.start, searchFocus.end); }
                catch (e) { /* be safe across input types */ }
            }
        }

        if (opts.focusRowId && opts.focusTable) {
            const target = container.querySelector(
                `table[data-table-type="${opts.focusTable}"] tr[id="${opts.focusRowId}"], ` +
                `table[data-table-type="${opts.focusTable}"] tr[data-ghost-row-id="${opts.focusRowId}"]`);
            if (target) target.scrollIntoView({ block: 'center' });
            else scrollToTableHeader(opts.focusTable, container);   // e.g. the row dropped out of the active filter
        } else if (opts.preserveScroll) {
            window.scrollTo(0, scrollY);
        } else if (opts.scrollToTable) {
            scrollToTableHeader(opts.scrollToTable, container);
        } else if (opts.scrollToTop) {   // legacy alias: the primary table's header
            scrollToTableHeader(primaryTable, container);
        }
    } catch (error) {
        if (seq !== renderSeq) return;
        container.innerHTML = `<div class="alert alert-danger">Error: ${error.message}</div>`;
        console.error('Failed to render table view:', error);
    }
}

/**
 * Re-render the current view after a mutation — page-bounded, so filter
 * membership, ghost overlays and pagination always reflect the journal.
 */
async function refreshView(focusRowId = null, focusTable = null) {
    await renderView(focusRowId
        ? { focusRowId, focusTable: focusTable || activeTable || primaryTable }
        : { preserveScroll: true });
}

// ── Delegated table interactions ─────────────────────────────────────────────
// One click listener on the persistent #tableContainer replaces per-item
// listeners on every .item-data span and every injected button.  Both
// tables of a paired session carry data-editable + data-table-type, so the
// same handlers serve either table; every interaction records the table it
// happened in (activeTable — the undo/redo target — and the table_type of
// the mutation payloads).

function fieldNameFromCell(td) {
    if (!td) return null;
    const classes = Array.from(td.classList);
    const fvIdx = classes.indexOf('field-value');
    return (fvIdx >= 0 && fvIdx + 1 < classes.length) ? classes[fvIdx + 1] : null;
}

function setupTableDelegation() {
    const container = document.getElementById('tableContainer');
    if (!container) return;  // not on the editor page

    container.addEventListener('click', function(e) {
        // Issue icons keep their inline onclick → highlightInvolvedElements
        if (e.target.closest('.issue-icon')) return;

        // Injected pager bars live outside the tables; data-pager carries the
        // table type — each table pages (and scrolls) independently.
        const pagerBtn = e.target.closest('.table-pager button[data-page]');
        if (pagerBtn) {
            e.stopPropagation();
            const target = parseInt(pagerBtn.dataset.page, 10) || 1;
            const kind = pagerBtn.closest('.table-pager').dataset.pager;
            const st = filteredStates[kind] || tableStates[kind];
            if (st) st.page = target;
            renderView({ scrollToTable: kind });
            return;
        }

        // Server-rendered per-table view controls
        const toggle = e.target.closest('[data-table-toggle]');
        if (toggle) {
            e.stopPropagation();
            const tt = toggle.dataset.tableType;
            setActiveTable(tt);
            const st = tableStates[tt];
            if (st) {
                if (toggle.dataset.tableToggle === 'show-all') st.showAllRows = !st.showAllRows;
                else st.changesOnly = !st.changesOnly;
                st.page = 1;
            }
            renderView({ scrollToTable: tt });
            return;
        }
        const exitBtn = e.target.closest('[data-exit-filter]');
        if (exitBtn) {
            e.stopPropagation();
            const tt = exitBtn.dataset.exitFilter;
            exitFilteredView(true, tt);
            renderView({ scrollToTable: tt });
            return;
        }
        const showAllBtn = e.target.closest('[data-empty-show-all]');
        if (showAllBtn) {
            e.stopPropagation();
            const tt = showAllBtn.dataset.emptyShowAll;
            setActiveTable(tt);
            const st = tableStates[tt];
            if (st) {
                st.showAllRows = true;
                st.page = 1;
            }
            renderView({ scrollToTable: tt });
            return;
        }

        // Per-table "Expand cells" button (client-injected into the controls
        // row, next to the two filter buttons) — pure DOM transform, no
        // refetch.  Lives outside the tables, so it must run before the
        // data-editable guard below.
        const cellToggle = e.target.closest('[data-cell-toggle]');
        if (cellToggle) {
            e.stopPropagation();
            const tt = cellToggle.dataset.tableType;
            setActiveTable(tt);
            if (tt && cellDisplay.modes[tt]) {
                cellDisplay.modes[tt] = cellDisplay.modes[tt] === 'expanded'
                    ? 'reduced' : 'expanded';
                // Global switch for this table = uniform state again.
                for (const key of Array.from(cellDisplay.rowOverrides.keys())) {
                    if (key.startsWith(tt + ':')) cellDisplay.rowOverrides.delete(key);
                }
                applyCellDisplay();
            }
            return;
        }

        // Issue-filter dropdown (server-rendered in the tools row).  Apply
        // reads the menu's checkboxes and enters the facet-filtered view;
        // Clear leaves it.  Both scroll to their own table.
        const facetApply = e.target.closest('[data-facet-apply]');
        if (facetApply) {
            e.stopPropagation();
            applyFacetFilter(facetApply.dataset.facetApply);
            return;
        }
        const facetClear = e.target.closest('[data-facet-clear]');
        if (facetClear) {
            e.stopPropagation();
            const tt = facetClear.dataset.facetClear;
            exitFilteredView(true, tt);
            renderView({ scrollToTable: tt });
            return;
        }

        const btn = e.target.closest('button');
        if (btn) {
            // Cell-display chevron — works on every table.
            if (btn.classList.contains('cell-toggle-btn')) {
                e.stopPropagation();
                toggleRowCellDisplay(btn);
                return;
            }
            if (!btn.closest('table[data-editable]')) return;
            e.stopPropagation();
            const tt = btn.closest('table')?.dataset.tableType || null;
            setActiveTable(tt);
            const tr = btn.closest('tr');
            const rowId = (tr && tr.id) ? tr.id : null;
            if (btn.classList.contains('add-row-btn')) { addNewRow(tt); return; }
            if (btn.classList.contains('delete-row-btn')) {
                if (rowId) deleteRow(rowId, tt);
                return;
            }
            const fieldName = fieldNameFromCell(btn.closest('td.field-value'));
            if (btn.classList.contains('clear-cell-btn')) {
                if (rowId && fieldName) clearCell(rowId, fieldName, tt);
                return;
            }
            if (btn.classList.contains('add-item-btn') || btn.classList.contains('add-first-item-btn')) {
                if (rowId && fieldName) openAddItemModal(rowId, fieldName, tt);
                return;
            }
            return;  // unknown button — do nothing
        }

        // Click on a value → open the edit modal
        const item = e.target.closest('.item-data');
        if (item) {
            const table = item.closest('table[data-table-type]');
            if (!table || !table.hasAttribute('data-editable')) return;
            const itemContainer = item.closest('.item-container');
            if (!itemContainer || !itemContainer.id) return;
            e.stopPropagation();
            e.preventDefault();
            const tt = table.dataset.tableType;
            setActiveTable(tt);
            currentItemTable = tt;
            currentItemId = itemContainer.id;
            openEditModal(item.textContent, currentItemId,
                          fieldNameFromCell(item.closest('td.field-value')));
        }
    });

    // Severity group checkbox in the issue-filter dropdown: select/deselect
    // all of its labels (pure client convenience — no fetch until Apply).
    container.addEventListener('change', e => {
        const group = e.target.closest('[data-facet-group]');
        if (!group) return;
        const grp = group.closest('.facet-group');
        if (!grp) return;
        grp.querySelectorAll('input[data-label]').forEach(i => {
            i.checked = group.checked;
        });
    });

    // Search box (server-rendered per table in the .table-tools row): the
    // server filters all rows of the table (pagination-friendly full-text
    // search) and re-renders the input state-correct; focus/caret are
    // restored by renderView.  The native × clear also fires 'input' with ''.
    const onSearchInput = debounce(e => {
        const input = e.target.closest('[data-search-input]');
        if (!input) return;
        const tt = input.dataset.searchInput;
        if (!tt) return;
        setActiveTable(tt);
        tableStates[tt].q = input.value;
        (filteredStates[tt] || tableStates[tt]).page = 1;
        renderView({ preserveScroll: true });
    }, 300);
    container.addEventListener('input', onSearchInput);

    // Lazy popover/tooltip initialisation: construct the Bootstrap instance
    // on first hover (and show it immediately, since the mouse is already
    // over the element and the instance's own listeners start from the next
    // hover cycle).
    container.addEventListener('mouseover', function(e) {
        const el = e.target.closest('[data-bs-toggle="popover"], [data-bs-toggle="tooltip"]');
        if (!el || el.dataset.lazyInit) return;
        el.dataset.lazyInit = '1';
        if (el.getAttribute('data-bs-toggle') === 'popover') {
            const popover = new bootstrap.Popover(el);
            popover.show();
        } else {
            new bootstrap.Tooltip(el);
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    initBootstrapWidgets();
    setupTableDelegation();
});


// ── Reduced / expanded cell display ──────────────────────────────────────────
// Purely client-side: the server always ships complete row markup, so the
// reduced view is a reversible DOM transform re-applied after every render.
//   • fully-valid cells → single truncated line (CSS text-overflow, class
//     'cell-collapsed', no DOM surgery);
//   • cells containing issues → every .item-container that holds an issue
//     icon stays fully visible; maximal runs of valid containers are wrapped
//     in a hidden span, one '…' marker per run.
// The restore step makes the transform idempotent and reversible (toggling
// never needs a server refetch).  Hidden runs can never contain an issue
// icon — icon-bearing containers are run-breakers — so the lazy popover
// delegation is unaffected by construction.

/** 'meta' or 'cits' — every table carries data-table-type (the two tables
 *  of a paired session reuse the same row ids, so the scope disambiguates
 *  cell-display override keys). */
function tableScope(table) {
    return table.dataset.tableType
        || (table.hasAttribute('data-editable') ? 'meta' : 'cits');
}

/** The mode a row currently uses: its override, else its table's mode. */
function effectiveRowMode(tr, scope) {
    return cellDisplay.rowOverrides.get(scope + ':' + tr.id)
        || cellDisplay.modes[scope] || 'reduced';
}

/** Undo any previous reduction of the row's cells (idempotent). */
function restoreRowCells(tr) {
    tr.querySelectorAll('td.field-value').forEach(td => {
        td.classList.remove('cell-collapsed', 'cell-collapsed-invalid');
        td.querySelectorAll(':scope > .cell-ellipsis').forEach(marker => marker.remove());
        td.querySelectorAll(':scope > .hidden-items-run').forEach(wrapper => {
            while (wrapper.firstChild) td.insertBefore(wrapper.firstChild, wrapper);
            wrapper.remove();
        });
    });
}

/** A hideable node: a span.item-container with no issue icons inside (a
 *  valid item — includes deleted-ghost items and icon-less empty slots). */
function isHideableContainer(node) {
    return node.nodeType === Node.ELEMENT_NODE && node.tagName === 'SPAN'
        && node.classList.contains('item-container')
        && !node.querySelector('.issue-icon');
}

function isPureWhitespaceText(node) {
    return node.nodeType === Node.TEXT_NODE && node.textContent.trim() === '';
}

/** Apply the reduced presentation to the row's cells (call after restore). */
function reduceRowCells(tr) {
    tr.querySelectorAll('td.field-value').forEach(td => {
        if (!td.querySelector('.issue-icon')) {
            // Fully-valid cell: CSS-only single line + ellipsis.
            td.classList.add('cell-collapsed');
            return;
        }
        // Mixed cell: hide maximal runs of valid containers (plus any
        // whitespace between them) behind an ellipsis marker; injected
        // action buttons and every icon-bearing container stay in place.
        const nodes = Array.from(td.childNodes);
        let i = 0;
        while (i < nodes.length) {
            if (!isHideableContainer(nodes[i])) { i++; continue; }
            let j = i;
            while (j + 1 < nodes.length
                   && (isHideableContainer(nodes[j + 1]) || isPureWhitespaceText(nodes[j + 1]))) {
                j++;
            }
            const run = nodes.slice(i, j + 1);
            const count = run.filter(isHideableContainer).length;
            const marker = document.createElement('span');
            marker.className = 'cell-ellipsis';
            marker.textContent = '…';
            marker.title = `${count} more item${count !== 1 ? 's' : ''} hidden — expand the row to show them`;
            const wrapper = document.createElement('span');
            wrapper.className = 'hidden-items-run';
            wrapper.hidden = true;
            td.insertBefore(marker, run[0]);
            run.forEach(node => wrapper.appendChild(node));   // moves the nodes in
            td.insertBefore(wrapper, marker);                  // final order: wrapper, marker
            i = j + 1;
        }
        td.classList.add('cell-collapsed-invalid');   // keeps normal wrapping
    });
}

/** Inject the per-row expand/reduce chevron (ghost rows stay untouched). */
function ensureRowToggleButton(tr) {
    if (tr.hasAttribute('data-ghost-row-id')) return;   // read-only ghost row
    const numCell = tr.querySelector('td.row-number');
    if (!numCell || numCell.querySelector('.cell-toggle-btn')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell-toggle-btn table-action';
    const delBtn = numCell.querySelector('.delete-row-btn');
    if (delBtn) numCell.insertBefore(btn, delBtn);
    else numCell.appendChild(btn);
    // icon/title are set by updateRowToggleButton on the same pass
}

/** Sync the chevron icon + tooltip with the row's effective mode. */
function updateRowToggleButton(tr, mode) {
    const btn = tr.querySelector('td.row-number .cell-toggle-btn');
    if (!btn) return;
    if (mode === 'expanded') {
        btn.textContent = '▾';
        btn.title = 'Reduce cell contents (show less)';
    } else {
        btn.textContent = '▸';
        btn.title = 'Expand cell contents (show full text)';
    }
}

/** Restore, then apply the row's effective mode (idempotent). */
function applyRowCellDisplay(tr, scope) {
    restoreRowCells(tr);
    const mode = effectiveRowMode(tr, scope);
    updateRowToggleButton(tr, mode);
    if (mode === 'expanded') return;
    reduceRowCells(tr);
}

/** Client-inject the per-table "Expand cells" button into each controls row
 *  (next to the two server-rendered filter buttons) — one per table, working
 *  independently.  Runs on every render before the state sync below. */
function ensureCellToggleButtons() {
    document.querySelectorAll('#tableContainer .table-controls').forEach(div => {
        const tt = div.dataset.tableType;
        if (!tt || div.querySelector('[data-cell-toggle]')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-sm btn-outline-secondary view-toggle';
        btn.dataset.cellToggle = '1';
        btn.dataset.tableType = tt;
        btn.textContent = 'Expand cells';
        btn.title = 'Reduced cells show one line per valid cell (invalid items '
                  + 'always in full); click to show this table’s full cell contents';
        div.appendChild(btn);
    });
}

/** Sync each table's "Expand cells" button with that table's mode. */
function updateCellToggleButtons() {
    document.querySelectorAll('#tableContainer [data-cell-toggle]').forEach(btn => {
        btn.classList.toggle('active',
                             cellDisplay.modes[btn.dataset.tableType] === 'expanded');
    });
}

/** Full pass over the current DOM — the render hook and the table toggles. */
function applyCellDisplay() {
    ensureCellToggleButtons();
    document.querySelectorAll('#tableContainer .table-container table').forEach(table => {
        const scope = tableScope(table);
        table.querySelectorAll('tbody tr').forEach(tr => {   // '+ row' is a button, not a tr
            ensureRowToggleButton(tr);
            applyRowCellDisplay(tr, scope);
        });
    });
    updateCellToggleButtons();
}

/** Per-row chevron click: flip this row's override and re-apply, no refetch. */
function toggleRowCellDisplay(btn) {
    const tr = btn.closest('tr');
    const table = btn.closest('table');
    if (!tr || !table || !tr.id) return;
    const scope = tableScope(table);
    const key = scope + ':' + tr.id;
    const current = cellDisplay.rowOverrides.get(key)
        || cellDisplay.modes[scope] || 'reduced';
    cellDisplay.rowOverrides.set(key, current === 'reduced' ? 'expanded' : 'reduced');
    applyRowCellDisplay(tr, scope);
}

/** Drop per-row overrides (revalidate renumbers row ids per generation). */
function resetCellDisplayOverrides() {
    cellDisplay.rowOverrides.clear();
}


// ── Issue-filtered view functionality ─────────────────────────────────────────

/**
 * Bridge function called by onclick attributes on .issue-icon spans.
 * The oc_validator package generates onclick="highlightInvolvedElements(this)"
 * so we keep this function name but redirect to the filtered view behavior.
 * (The initBootstrapWidgets override above usually wins — this standalone
 * version is a fallback.)
 *
 * @param {HTMLElement} clickedIssue  The .issue-icon span that was clicked.
 */
function highlightInvolvedElements(clickedIssue) {
    const table = clickedIssue.closest('table[data-table-type]');
    if (!table || !table.hasAttribute('data-editable')) return;
    const issueId = clickedIssue.id;
    if (!issueId) {
        console.warn('Issue icon has no id attribute');
        return;
    }
    loadFilteredTable(issueId, table.dataset.tableType);
}

/**
 * Switch one table to the filtered view showing only rows involved in the
 * given issue (paginated).  That table's current view state (page + both
 * toggles) is saved and restored verbatim by exitFilteredViewAndReload();
 * the other table is unaffected.
 *
 * @param {string} issueId    The issue ID (e.g., 'meta-0', 'cits-1')
 * @param {string} tableType  'meta' or 'cits'
 */
async function loadFilteredTable(issueId, tableType = null) {
    if (!window.currentSessionId) {
        console.error('Session ID not available for filtered view');
        return;
    }
    const tt = tableType || activeTable || primaryTable;
    setActiveTable(tt);
    if (filteredStates[tt] && filteredStates[tt].issueId === issueId) {
        await refreshView(null, tt);  // already filtering on this issue — refresh
        return;
    }
    if (!filteredStates[tt]) savedStates[tt] = { ...tableStates[tt] };
    filteredStates[tt] = { issueId, page: 1, types: [] };
    await renderView({ scrollToTable: tt });
}

/**
 * Leave the filtered view.  With ``restore`` (default) the saved view state
 * is restored; without it the state is dropped (used after revalidation,
 * when issue ids are renumbered).  ``tableType`` limits the exit to one
 * table; without it every filtered table exits.
 */
function exitFilteredView(restore = true, tableType = null) {
    for (const tt of (tableType ? [tableType] : ['meta', 'cits'])) {
        if (filteredStates[tt]) {
            if (restore && savedStates[tt]) Object.assign(tableStates[tt], savedStates[tt]);
            else if (restore) tableStates[tt].page = 1;
            savedStates[tt] = null;
            filteredStates[tt] = null;
        }
    }
}

/**
 * Exit filtered view and return to the exact view the user left (page
 * number + both toggle states).  The exit buttons are server-rendered with
 * each table's banner (delegated via [data-exit-filter]).
 */
function exitFilteredViewAndReload(tableType = null) {
    const tt = tableType || activeTable || primaryTable;
    exitFilteredView(true, tt);
    renderView({ scrollToTable: tt });
}

/**
 * Read the checked boxes of one table's issue-filter dropdown and enter
 * (or, when nothing is checked, leave) the facet-filtered view — rows
 * carrying at least one issue of a selected type.  The severity group
 * checkboxes are a select-all for their labels, so "all errors" is one
 * click.  Entering facets while a single-issue filter is active replaces
 * it (and vice versa) without re-capturing the saved state, so "Back to
 * full table" always restores the view the user started from.
 */
function applyFacetFilter(tt) {
    const container = document.getElementById('tableContainer');
    const menu = container && container.querySelector(
        `.facet-menu[data-table-type="${tt}"]`);
    if (!menu) return;
    setActiveTable(tt);
    const types = [...new Set(
        [...menu.querySelectorAll('input[data-label]:checked')]
            .map(i => i.dataset.label))];
    if (!types.length) {
        exitFilteredView(true, tt);
    } else {
        if (!filteredStates[tt]) savedStates[tt] = { ...tableStates[tt] };
        filteredStates[tt] = { issueId: null, page: 1, types };
    }
    renderView({ scrollToTable: tt });
}


// ── General utilities ─────────────────────────────────────────────────────────

// Utility function to show alerts
function showAlert(message, type = 'info', duration = 3000) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.setAttribute('role', 'alert');
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;

    // Add to top of container
    const container = document.querySelector('.container-fluid');
    if (container) {
        container.insertBefore(alertDiv, container.firstChild);
    }

    // Auto-dismiss
    setTimeout(() => {
        alertDiv.classList.remove('show');
        setTimeout(() => alertDiv.remove(), 150);
    }, duration);
}

// Format date for display
function formatDate(dateString) {
    if (!dateString) return '-';
    const date = new Date(dateString);
    return date.toLocaleString();
}

// Debounce function for performance
function debounce(func, wait) {
    let timeout;
    return function(...args) {
        const context = this;
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(context, args), wait);
    };
}

// Copy text to clipboard
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        showAlert('Copied to clipboard!', 'success');
    } catch (err) {
        console.error('Failed to copy:', err);
        showAlert('Failed to copy to clipboard', 'error');
    }
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        initBootstrapWidgets,
        renderView,
        refreshView,
        applyCellDisplay,
        resetCellDisplayOverrides,
        highlightInvolvedElements,
        loadFilteredTable,
        exitFilteredView,
        exitFilteredViewAndReload,
        showAlert,
        formatDate,
        debounce,
        copyToClipboard
    };
}
