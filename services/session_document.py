"""Session locks and HTML string-surgery utilities.

After the event-sourced-journal refactor this module no longer caches parsed
trees — the baseline HTML is immutable and views are computed by
``services/view_builder.py`` from string splices.  What remains here:

- ``atomic_write`` — temp-file + ``os.replace`` with ``newline=''`` so saved
  strings are byte-stable (used for every file the app writes);
- ``find_row_bounds`` / ``replace_row_str`` / ``remove_row_str`` /
  ``insert_row_str`` — quote-anchored ``<tr>`` location and splicing (safe
  against ``row5``/``row50`` prefix collisions and ``data-ghost-row-id``
  look-alikes, tolerant of attribute order);
- ``compose_display`` — derives the paired-session display document from the
  two per-table HTML strings;
- ``DocumentCache`` — the per-session ``asyncio.Lock`` factory.  Any journal
  or view read/write must hold ``document_cache.session_lock(session_id)``.
"""
import asyncio
from pathlib import Path
from typing import Optional

from aiofiles import open as aio_open
from aiofiles.os import replace as aio_os_replace

from config import TEMP_DIR


class SpliceError(Exception):
    """Raised when a row cannot be located in an HTML string."""


# ---------------------------------------------------------------------------
# File-name scheme (single source of truth; SessionManager delegates here)
# ---------------------------------------------------------------------------

_HTML_FILENAMES: dict = {
    'meta': 'meta_table.html',
    'cits': 'cits_table.html',
    'display': 'meta_html.html',
    'baseline_meta': 'baseline_meta.html',
    'baseline_cits': 'baseline_cits.html',
}


def filename_for(table_type: str) -> str:
    """Return the on-disk filename for a table_type key (incl. baselines)."""
    fname = _HTML_FILENAMES.get(table_type)
    if fname is None:
        raise ValueError(
            f"Unknown table_type '{table_type}'. "
            f"Expected one of: {list(_HTML_FILENAMES.keys())}"
        )
    return fname


async def atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + os.replace).

    ``newline=''`` keeps the bytes byte-identical to the string (no Windows
    ``\\n`` -> ``\\r\\n`` translation).
    """
    tmp_path = path.with_name(path.name + '.tmp')
    async with aio_open(tmp_path, 'w', encoding='utf-8', newline='') as f:
        await f.write(content)
    await aio_os_replace(tmp_path, path)


# ---------------------------------------------------------------------------
# String-level row surgery
# ---------------------------------------------------------------------------

def find_row_bounds(html: str, row_id: str) -> tuple:
    """Return ``(start, end)`` offsets of the ``<tr id="row_id">...</tr>`` slice.

    The anchor is ``id="rowN"`` *including* the closing quote (without it,
    ``row5`` would prefix-match ``row50``).  The character before the anchor
    must be whitespace, which rejects look-alikes such as
    ``data-ghost-row-id="rowN"``.  Serialized files may have ``class``
    before ``id`` on the ``<tr>`` tag, so the tag start is located with
    ``rfind``.
    """
    anchor = f'id="{row_id}"'
    pos = html.find(anchor)
    while pos != -1:
        before = html[pos - 1] if pos > 0 else ' '
        if before.isspace():
            break
        pos = html.find(anchor, pos + 1)
    if pos == -1:
        raise SpliceError(f"Row '{row_id}' not found in document")
    start = html.rfind('<tr', 0, pos)
    if start == -1:
        raise SpliceError(f"Opening <tr> not found for row '{row_id}'")
    end = html.find('</tr>', pos)
    if end == -1:
        raise SpliceError(f"Closing </tr> not found for row '{row_id}'")
    return start, end + len('</tr>')


def replace_row_str(html: str, row_id: str, new_row_html: str) -> str:
    start, end = find_row_bounds(html, row_id)
    return html[:start] + new_row_html + html[end:]


def remove_row_str(html: str, row_id: str) -> str:
    start, end = find_row_bounds(html, row_id)
    return html[:start] + html[end:]


def insert_row_str(html: str, row_html: str, before_row_id: Optional[str]) -> str:
    """Insert a row before ``before_row_id`` (or before ``</tbody>`` if None)."""
    if before_row_id:
        try:
            start, _ = find_row_bounds(html, before_row_id)
            return html[:start] + row_html + html[start:]
        except SpliceError:
            pass  # anchor vanished — fall through to append
    idx = html.find('</tbody>')
    if idx == -1:
        raise SpliceError("No </tbody> found to insert row into")
    return html[:idx] + row_html + html[idx:]


# ---------------------------------------------------------------------------
# Display-file derivation (paired sessions)
# ---------------------------------------------------------------------------

def _slice_div(html: str, marker: str) -> str:
    """Extract a top-level ``<div>`` slice identified by a class marker.

    Neither ``general-info`` nor ``table-container`` contains nested
    ``<div>``s, so the first ``</div>`` after the marker closes the element.
    """
    pos = html.find(marker)
    if pos == -1:
        raise SpliceError(f"Marker '{marker}' not found in document")
    start = html.rfind('<div', 0, pos)
    end = html.find('</div>', pos)
    if start == -1 or end == -1:
        raise SpliceError(f"Div boundaries not found around '{marker}'")
    return html[start:end + len('</div>')]


def compose_display(meta_html: str, cits_html: str) -> str:
    """Rebuild the merged display document from the two table HTML strings.

    Equivalent to ``oc_validator.interface.gui.merge_html_files`` output: the
    citations ``general-info`` and ``table-container`` divs are inserted right
    after the metadata ``table-container`` div.
    """
    tc_marker = '<div class="table-container'
    gi = _slice_div(cits_html, 'container-fluid general-info')
    tc_cits = _slice_div(cits_html, tc_marker)
    tc_meta_start = meta_html.find(tc_marker)
    if tc_meta_start == -1:
        raise SpliceError("Meta table-container not found")
    tc_meta_end = meta_html.find('</div>', tc_meta_start) + len('</div>')
    return meta_html[:tc_meta_end] + gi + tc_cits + meta_html[tc_meta_end:]


# ---------------------------------------------------------------------------
# Per-session locks
# ---------------------------------------------------------------------------

class DocumentCache:
    """Per-session ``asyncio.Lock`` registry.

    Every journal operation and view computation (mutations, undo/redo,
    get_html, filtered/deleted views, commit, revalidate, export) must hold
    ``session_lock(session_id)`` so reads never interleave with journal
    appends.  Single-process app (single uvicorn worker) by design.
    """

    def __init__(self) -> None:
        self._locks: dict = {}

    def session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def drop_session(self, session_id: str) -> None:
        self._locks.pop(session_id, None)


# Module-level singleton
document_cache = DocumentCache()
