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

        if (!clickedIssue.closest('table[data-editable]')) return;  // read-only tables
        const issueId = clickedIssue.id;
        if (!issueId) {
            console.warn('Issue icon has no id attribute');
            return;
        }
        loadFilteredTable(issueId);
    };

    // Also override clearHighlights to do nothing (we don't use it anymore)
    window.clearHighlights = function() {};
}

// ── View state + rendering core ──────────────────────────────────────────────
// Every view of the table is one page (≤ 25 rows) fetched from
// GET /api/edit/table/{session_id}.  The toggles and the pager only mutate
// this state and re-render; mutations call refreshView() so filter
// membership, ghosts and page boundaries always come from the server.

const viewState = { page: 1, showAllRows: false, changesOnly: false };
const citsState = { page: 1 };     // read-only citations table pager (paired sessions)
let filteredState = null;          // {issueId, page} while in the issue-filtered view
let savedMainViewState = null;     // captured on entering the filtered view, restored on exit
let renderSeq = 0;                 // stale-response guard for overlapping fetches

// Cell display mode: 'reduced' (default) collapses fully-valid cells to one
// truncated line and hides valid items in error cells behind an ellipsis;
// 'expanded' shows full content.  Deliberately separate from viewState and
// never captured by savedMainViewState — the setting is shared between the
// main and issue-filtered views and independent of the view toggles.  Row
// overrides survive re-renders and pagination, keyed 'main:rowN' / 'cits:rowN'
// (the two tables of a paired session reuse the same row ids).
const cellDisplay = { mode: 'reduced', rowOverrides: new Map() };

/**
 * Fetch and inject the current view (main or issue-filtered).
 * opts: { preserveScroll, scrollToTop, focusRowId }
 */
async function renderView(opts = {}) {
    const container = document.getElementById('tableContainer');
    const sid = window.currentSessionId;
    if (!container || !sid) return;

    const params = new URLSearchParams();
    if (filteredState) {
        params.set('issue_id', filteredState.issueId);
        params.set('page', String(filteredState.page));
    } else {
        params.set('page', String(viewState.page));
        params.set('show_all', String(viewState.showAllRows));
        params.set('changes_only', String(viewState.changesOnly));
    }
    params.set('cits_page', String(citsState.page));
    if (opts.focusRowId) params.set('focus_row_id', opts.focusRowId);

    const seq = ++renderSeq;
    const scrollY = window.scrollY;
    try {
        const response = await fetch(`/api/edit/table/${sid}?${params.toString()}`);
        const data = await response.json();
        if (seq !== renderSeq) return;  // a newer request superseded this one
        if (!response.ok) throw new Error(data.detail || 'Failed to load table view');

        // Server-side page clamping is authoritative
        if (filteredState) filteredState.page = data.page;
        else viewState.page = data.page;
        if (data.cits) citsState.page = data.cits.page;

        if (typeof disposePopoversIn === 'function') disposePopoversIn(container);
        container.innerHTML = (data.paginated && data.total_rows === 0)
            ? emptyStateHtml() + data.html
            : data.html;
        if (typeof setupEditHandlers === 'function') setupEditHandlers();
        applyCellDisplay();   // after enhanceRow (buttons exist) and before the focus scroll
        initBootstrapWidgets();
        updateViewToggleButtons();
        const toggles = document.getElementById('viewToggles');
        if (toggles) toggles.style.display = data.has_table === false ? 'none' : '';
        if (filteredState) showFilterBanner(filteredState.issueId, data.total_rows);

        if (opts.focusRowId) {
            const target = container.querySelector(
                `table[data-editable] tr[id="${opts.focusRowId}"], ` +
                `table[data-editable] tr[data-ghost-row-id="${opts.focusRowId}"]`);
            if (target) target.scrollIntoView({ block: 'center' });
        } else if (opts.preserveScroll) {
            window.scrollTo(0, scrollY);
        } else if (opts.scrollToTop) {
            container.scrollIntoView({ block: 'start' });
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
async function refreshView(focusRowId = null) {
    await renderView(focusRowId
        ? { focusRowId }
        : { preserveScroll: true });
}

/** Context-dependent message shown above an empty table. */
function emptyStateHtml() {
    if (filteredState) {
        return `<div class="empty-state">No rows involved in this issue are left ` +
               `in the table (deleted rows drop out of the filtered view).</div>`;
    }
    if (viewState.changesOnly) {
        return `<div class="empty-state">No changes yet — edit, add or delete content, ` +
               `or turn off “Show Changes Only”.</div>`;
    }
    if (!viewState.showAllRows) {
        return `<div class="empty-state">No rows with errors/warnings or changes.` +
               `<button type="button" class="btn btn-sm btn-outline-primary ms-2" ` +
               `onclick="enableShowAllRows()">Show all rows</button></div>`;
    }
    return `<div class="empty-state">The table is empty.</div>`;
}

/** "Show all rows" call-to-action inside the empty state. */
function enableShowAllRows() {
    viewState.showAllRows = true;
    viewState.page = 1;
    renderView({ scrollToTop: true });
}

/** Sync the toolbar toggles with the state (disabled inside the filtered view). */
function updateViewToggleButtons() {
    const allBtn = document.getElementById('showAllRowsBtn');
    const chBtn = document.getElementById('showChangesOnlyBtn');
    if (allBtn) {
        allBtn.classList.toggle('active', viewState.showAllRows);
        allBtn.disabled = !!filteredState;
    }
    if (chBtn) {
        chBtn.classList.toggle('active', viewState.changesOnly);
        chBtn.disabled = !!filteredState;
    }
    const cdBtn = document.getElementById('cellDisplayBtn');
    if (cdBtn) {
        // Shared by the main and filtered views — intentionally never disabled.
        cdBtn.classList.toggle('active', cellDisplay.mode === 'expanded');
    }
}

// ── Delegated table interactions ─────────────────────────────────────────────
// One click listener on the persistent #tableContainer replaces per-item
// listeners on every .item-data span and every injected button.  Only the
// editable table (marked data-editable by the backend) reacts — the paired
// session's read-only citations table shares the markup but no handlers.

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

        // Injected pager bars (meta + cits) live outside the tables
        const pagerBtn = e.target.closest('.table-pager button[data-page]');
        if (pagerBtn) {
            e.stopPropagation();
            const target = parseInt(pagerBtn.dataset.page, 10) || 1;
            const kind = pagerBtn.closest('.table-pager').dataset.pager;
            if (kind === 'cits') citsState.page = target;
            else if (filteredState) filteredState.page = target;
            else viewState.page = target;
            renderView({ scrollToTop: true });
            return;
        }

        const btn = e.target.closest('button');
        if (btn) {
            // Cell-display chevron — must run before the data-editable guard
            // below so it also works in the read-only citations table.
            if (btn.classList.contains('cell-toggle-btn')) {
                e.stopPropagation();
                toggleRowCellDisplay(btn);
                return;
            }
            if (!btn.closest('table[data-editable]')) return;
            e.stopPropagation();
            const tr = btn.closest('tr');
            const rowId = (tr && tr.id) ? tr.id : null;
            if (btn.classList.contains('add-row-btn')) { addNewRow(); return; }
            if (btn.classList.contains('delete-row-btn')) {
                if (rowId) deleteRow(rowId);
                return;
            }
            const fieldName = fieldNameFromCell(btn.closest('td.field-value'));
            if (btn.classList.contains('clear-cell-btn')) {
                if (rowId && fieldName) clearCell(rowId, fieldName);
                return;
            }
            if (btn.classList.contains('add-item-btn') || btn.classList.contains('add-first-item-btn')) {
                if (rowId && fieldName) openAddItemModal(rowId, fieldName);
                return;
            }
            return;  // unknown button — do nothing
        }

        // Click on a value → open the edit modal
        const item = e.target.closest('.item-data');
        if (item) {
            if (!item.closest('table[data-editable]')) return;
            const itemContainer = item.closest('.item-container');
            if (!itemContainer || !itemContainer.id) return;
            e.stopPropagation();
            e.preventDefault();
            currentItemId = itemContainer.id;
            openEditModal(item.textContent, currentItemId);
        }
    });

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

/** 'main' for the editable table, 'cits' for the read-only citations table. */
function tableScope(table) {
    return table.hasAttribute('data-editable') ? 'main' : 'cits';
}

/** The mode a row currently uses: its override, else the table-wide mode. */
function effectiveRowMode(tr, scope) {
    return cellDisplay.rowOverrides.get(scope + ':' + tr.id) || cellDisplay.mode;
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

/** Full pass over the current DOM — the render hook and the toolbar toggle. */
function applyCellDisplay() {
    document.querySelectorAll('#tableContainer .table-container table').forEach(table => {
        const scope = tableScope(table);
        table.querySelectorAll('tbody tr').forEach(tr => {   // '+ row' is a button, not a tr
            ensureRowToggleButton(tr);
            applyRowCellDisplay(tr, scope);
        });
    });
}

/** Per-row chevron click: flip this row's override and re-apply, no refetch. */
function toggleRowCellDisplay(btn) {
    const tr = btn.closest('tr');
    const table = btn.closest('table');
    if (!tr || !table || !tr.id) return;
    const scope = tableScope(table);
    const key = scope + ':' + tr.id;
    const current = cellDisplay.rowOverrides.get(key) || cellDisplay.mode;
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
 *
 * @param {HTMLElement} clickedIssue  The .issue-icon span that was clicked.
 */
function highlightInvolvedElements(clickedIssue) {
    // The read-only citations table shares the icon markup — only the
    // editable table's icons open the filtered view.
    if (!clickedIssue.closest('table[data-editable]')) return;
    const issueId = clickedIssue.id;
    if (!issueId) {
        console.warn('Issue icon has no id attribute');
        return;
    }
    loadFilteredTable(issueId);
}

/**
 * Switch to the filtered view showing only rows involved in the given issue
 * (paginated).  The current main-view state (page + both toggles) is saved
 * and restored verbatim by exitFilteredViewAndReload().
 *
 * @param {string} issueId  The issue ID (e.g., 'meta-0', 'cits-1')
 */
async function loadFilteredTable(issueId) {
    if (!window.currentSessionId) {
        console.error('Session ID not available for filtered view');
        return;
    }
    if (filteredState && filteredState.issueId === issueId) {
        await refreshView();  // already filtering on this issue — just refresh
        return;
    }
    savedMainViewState = { ...viewState };
    filteredState = { issueId, page: 1 };
    await renderView({ scrollToTop: true });
}

/**
 * Leave the filtered view.  With ``restore`` (default) the saved main-view
 * state is restored; without it the state is dropped (used after
 * revalidation, when issue ids are renumbered).
 */
function exitFilteredView(restore = true) {
    if (filteredState) {
        if (restore && savedMainViewState) Object.assign(viewState, savedMainViewState);
        else if (restore) viewState.page = 1;
        savedMainViewState = null;
        filteredState = null;
    }
    const banner = document.getElementById('filterBanner');
    if (banner) {
        banner.style.display = 'none';
    }
}

/**
 * Show the filter banner with issue ID and row count.
 */
function showFilterBanner(issueId, rowCount) {
    let banner = document.getElementById('filterBanner');

    if (!banner) {
        // Create banner if it doesn't exist (fallback for templates that don't have it)
        const cardHeader = document.querySelector('#tableContainer').closest('.card').querySelector('.card-header');
        if (cardHeader) {
            banner = document.createElement('div');
            banner.id = 'filterBanner';
            banner.className = 'filter-banner';
            cardHeader.after(banner);
        }
    }

    if (banner) {
        banner.innerHTML = `
            <div class="filter-banner-content">
                <button type="button" class="btn btn-sm btn-outline-primary" onclick="exitFilteredViewAndReload()">
                    ← Back to full table
                </button>
                <span class="filter-banner-text">
                    Filtered by issue: <strong>${issueId}</strong>
                </span>
                <span class="badge bg-secondary">${rowCount} row${rowCount !== 1 ? 's' : ''}</span>
            </div>
        `;
        banner.style.display = 'block';
    }
}

/**
 * Exit filtered view and return to the exact main view the user left
 * (page number + both toggle states).  Called from the banner button.
 */
function exitFilteredViewAndReload() {
    exitFilteredView();
    renderView({ scrollToTop: true });
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
        emptyStateHtml,
        enableShowAllRows,
        updateViewToggleButtons,
        applyCellDisplay,
        resetCellDisplayOverrides,
        highlightInvolvedElements,
        loadFilteredTable,
        exitFilteredView,
        showFilterBanner,
        exitFilteredViewAndReload,
        showAlert,
        formatDate,
        debounce,
        copyToClipboard
    };
}
