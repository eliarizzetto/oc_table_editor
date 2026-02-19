/* OC Table Editor - JavaScript Utilities */

// ── Bootstrap widget initialisation ──────────────────────────────────────────
// Exposed as a named function so it can be re-called after dynamic HTML injection
// (the table HTML is loaded asynchronously, so DOMContentLoaded is too early).

function initBootstrapWidgets() {
    // Enable Bootstrap popovers for issue icons
    const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.map(function(el) {
        // Dispose any existing popover instance before creating a new one to avoid duplicates
        const existing = bootstrap.Popover.getInstance(el);
        if (existing) existing.dispose();
        return new bootstrap.Popover(el);
    });

    // Enable Bootstrap tooltips
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function(el) {
        const existing = bootstrap.Tooltip.getInstance(el);
        if (existing) existing.dispose();
        return new bootstrap.Tooltip(el);
    });

    // Override the highlightInvolvedElements function that oc_validator embeds in the HTML.
    // The embedded script runs after HTML injection and defines its own version,
    // so we must override it here to redirect to our filtered-view behavior.
    window.highlightInvolvedElements = function(clickedIssue) {
        console.log('OVERRIDE highlightInvolvedElements called for:', clickedIssue.id);
        
        // Hide the popover immediately so it doesn't cover the filtered view
        const popover = bootstrap.Popover.getInstance(clickedIssue);
        if (popover) {
            popover.hide();
        }
        
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

document.addEventListener('DOMContentLoaded', function() {
    initBootstrapWidgets();
});


// ── Issue-filtered view functionality ─────────────────────────────────────────

let currentFilterIssueId = null;

/**
 * Bridge function called by onclick attributes on .issue-icon spans.
 * The oc_validator package generates onclick="highlightInvolvedElements(this)"
 * so we keep this function name but redirect to the filtered view behavior.
 *
 * @param {HTMLElement} clickedIssue  The .issue-icon span that was clicked.
 */
function highlightInvolvedElements(clickedIssue) {
    // Extract the issue ID from the clicked element's id attribute
    const issueId = clickedIssue.id;
    if (!issueId) {
        console.warn('Issue icon has no id attribute');
        return;
    }
    loadFilteredTable(issueId);
}

/**
 * Load a filtered table view showing only rows involved in the given issue.
 * Called via onclick attribute on .issue-icon spans.
 *
 * @param {string} issueId  The issue ID (e.g., 'meta-0', 'cits-1')
 */
async function loadFilteredTable(issueId) {
    const container = document.getElementById('tableContainer');
    const sessionId = window.currentSessionId || (typeof sessionId !== 'undefined' ? sessionId : null);

    if (!sessionId) {
        console.error('Session ID not available for filtered view');
        return;
    }

    // Show loading state
    container.innerHTML = `
        <div class="text-center py-5">
            <div class="spinner-border" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-2">Loading filtered view...</p>
        </div>
    `;

    try {
        const response = await fetch('/api/edit/filtered-rows', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                issue_id: issueId
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || 'Failed to load filtered rows');
        }

        // Inject filtered HTML
        container.innerHTML = data.html;
        currentFilterIssueId = issueId;

        // Re-attach edit handlers and Bootstrap widgets
        if (typeof setupEditHandlers === 'function') {
            setupEditHandlers();
        }
        initBootstrapWidgets();

        // Show filter banner
        showFilterBanner(issueId, data.row_indices.length);

    } catch (error) {
        container.innerHTML = `<div class="alert alert-danger">Error: ${error.message}</div>`;
        console.error('Failed to load filtered table:', error);
    }
}

/**
 * Exit the filtered view and return to the full table.
 * This only clears the filter state; callers must call loadTable() separately.
 */
function exitFilteredView() {
    currentFilterIssueId = null;
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
 * Exit filtered view and reload the full table.
 * Called from the "Back to full table" button in the banner.
 */
function exitFilteredViewAndReload() {
    exitFilteredView();
    // loadTable is defined in editor.html inline script; call it if available
    if (typeof loadTable === 'function') {
        loadTable();
    }
}

/**
 * Reload the table, preserving the filter if one is active.
 * Use this after mutations (save, delete, undo, redo) to stay in filtered mode.
 */
async function reloadTable() {
    if (currentFilterIssueId) {
        await loadFilteredTable(currentFilterIssueId);
    } else if (typeof loadTable === 'function') {
        await loadTable();
    }
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
        highlightInvolvedElements,
        loadFilteredTable,
        exitFilteredView,
        showFilterBanner,
        exitFilteredViewAndReload,
        reloadTable,
        showAlert,
        formatDate,
        debounce,
        copyToClipboard
    };
}
