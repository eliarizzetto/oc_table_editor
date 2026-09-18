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
