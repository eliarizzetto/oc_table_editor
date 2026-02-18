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
}

document.addEventListener('DOMContentLoaded', function() {
    initBootstrapWidgets();
});


// ── Issue-highlight functionality (ported from oc_validator script.js) ────────

let currentHighlightedIssueId = null;

/**
 * Toggle-highlight all table cells involved in the same error instance as the
 * clicked issue-icon square.  Called via onclick attribute on .issue-icon spans
 * that are embedded in the oc_validator-generated HTML.
 *
 * @param {HTMLElement} clickedIssue  The .issue-icon span that was clicked.
 */
function highlightInvolvedElements(clickedIssue) {
    const clickedIssueId = clickedIssue.id;
    const issueColor = clickedIssue.style.backgroundColor;

    // Clicking the same issue again clears the highlights
    if (currentHighlightedIssueId === clickedIssueId) {
        clearHighlights();
        currentHighlightedIssueId = null;
        return;
    }

    // Clear any pre-existing highlights first
    clearHighlights();

    // Find all issue-icon spans with the same id inside the table
    const table = document.getElementById('table-data');
    if (!table) return;

    const matchingIssues = table.querySelectorAll(`.issue-icon#${CSS.escape(clickedIssueId)}`);

    matchingIssues.forEach(issue => {
        // The .item-data span is a sibling inside the same .item-container
        const container = issue.closest('.item-container');
        if (!container) return;
        const itemData = container.querySelector('.item-data');
        if (itemData) {
            itemData.style.backgroundColor = issueColor;
            itemData.classList.add('highlighted');

            // Give empty cells a visual placeholder so the highlight is visible
            if (itemData.textContent.trim() === '') {
                itemData.textContent = '(empty)';
                itemData.classList.add('empty-placeholder');
            }
        }
    });

    currentHighlightedIssueId = clickedIssueId;
}

/**
 * Remove all highlight styling applied by highlightInvolvedElements().
 */
function clearHighlights() {
    document.querySelectorAll('.item-data.highlighted').forEach(el => {
        el.style.backgroundColor = '';
        el.classList.remove('highlighted');

        if (el.classList.contains('empty-placeholder')) {
            el.textContent = '';
            el.classList.remove('empty-placeholder');
        }
    });
    currentHighlightedIssueId = null;
}

// Delegated click listener: clear highlights when the user clicks anywhere in
// the table that is NOT an issue-icon square.  Delegation is required because
// #table-data is injected dynamically and doesn't exist at script-load time.
document.addEventListener('click', function(event) {
    const table = document.getElementById('table-data');
    if (!table) return;
    if (table.contains(event.target) && !event.target.classList.contains('issue-icon')) {
        clearHighlights();
    }
});


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
        clearHighlights,
        showAlert,
        formatDate,
        debounce,
        copyToClipboard
    };
}
