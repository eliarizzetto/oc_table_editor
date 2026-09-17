"""In-memory document cache with row-splice persistence.

The HTML file on disk remains the source of truth, but instead of parsing and
re-serialising the whole document on every request this module keeps, per
session/table:

- ``canonical``: the exact HTML string that is served to clients and written
  to disk (kept authoritative at all times);
- ``soup``: a parsed BeautifulSoup tree, built lazily (only when a mutation
  actually needs it) and kept in sync with ``canonical`` via row splices.

Every mutation is row-scoped: the tree is mutated with the existing DOM
surgery helpers, only the affected ``<tr>`` is re-serialised, and the row is
*spliced* back into ``canonical`` by string surgery.  Full-document parses
(9-12 s on a 12 MB table) and serialisations (~2.8 s) therefore happen at most
once per session (bootstrap), never per click.

Invariants (enforced by design):
- ``canonical`` updates are always single synchronous statements, so async
  readers never observe a torn document.
- Mutation flows must hold the per-session lock
  (``DocumentCache.session_lock``) while touching the tree / canonical.
- On any exception after a tree mutation begins, evict the whole document
  (``DocumentCache.drop_document``) — disk holds the pre-mutation state (all
  writes are atomic) and an undo snapshot was pushed before the mutation.

This cache is per-process: the app must run under a single uvicorn worker
(the bundled dev server does).
"""
import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from aiofiles import open as aio_open
from aiofiles.os import replace as aio_os_replace
from bs4 import BeautifulSoup

from config import TEMP_DIR

# Cache bounds.  Strings are cheap (one per table); soup trees are not
# (~316 MB for a 12 MB document), so they get a much smaller LRU.
MAX_CACHED_DOCUMENTS = 8
MAX_CACHED_SOUPS = 2


class SpliceError(Exception):
    """Raised when a row cannot be located in the canonical HTML string."""


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
    ``\\n`` -> ``\\r\\n`` translation), so canonical strings stay stable
    across save/load cycles.
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
    ``data-ghost-row-id="rowN"``.  Real files may serialise ``class`` before
    ``id`` on the ``<tr>`` tag, so the tag start is located with ``rfind``.
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


def next_row_id_in_str(html: str, row_id: str) -> Optional[str]:
    """Id of the first ``<tr>`` that appears after ``row_id`` in the document."""
    import re
    try:
        _, end = find_row_bounds(html, row_id)
    except SpliceError:
        return None
    m = re.search(r'<tr[^>]*\sid="([^"]+)"', html[end:end + 2000])
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Display-file derivation (paired sessions)
# ---------------------------------------------------------------------------

def _slice_div(html: str, first_class: str, marker: str) -> str:
    """Extract a top-level ``<div>`` slice identified by a class marker.

    ``marker`` locates the div; ``first_class`` is only used for a sanity
    check that the located tag really is the wanted div.  Neither
    ``general-info`` nor ``table-container`` contains nested ``<div>``s, so
    the first ``</div>`` after the marker closes the element.
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
    """Rebuild the merged display document from the two canonical tables.

    Equivalent to ``oc_validator.interface.gui.merge_html_files`` output: the
    citations ``general-info`` and ``table-container`` divs are inserted right
    after the metadata ``table-container`` div.  Deriving (instead of storing
    and patching) means edits and undo/redo are always reflected immediately.
    """
    tc_marker = '<div class="table-container'
    gi = _slice_div(cits_html, 'general-info', 'container-fluid general-info')
    tc_cits = _slice_div(cits_html, 'table-container', tc_marker)
    tc_meta_start = meta_html.find(tc_marker)
    if tc_meta_start == -1:
        raise SpliceError("Meta table-container not found")
    tc_meta_end = meta_html.find('</div>', tc_meta_start) + len('</div>')
    return meta_html[:tc_meta_end] + gi + tc_cits + meta_html[tc_meta_end:]


# ---------------------------------------------------------------------------
# Session document
# ---------------------------------------------------------------------------

class SessionDocument:
    """Canonical HTML string + lazily-parsed tree for one session table."""

    def __init__(self, cache: 'DocumentCache', session_id: str, table_type: str,
                 canonical: str):
        self._cache = cache
        self.session_id = session_id
        self.table_type = table_type
        self.canonical = canonical
        self._soup: Optional[BeautifulSoup] = None

    @property
    def soup(self) -> Optional[BeautifulSoup]:
        return self._soup

    @property
    def path(self) -> Path:
        return TEMP_DIR / self.session_id / filename_for(self.table_type)

    async def persist(self) -> None:
        """Atomically write ``canonical`` to disk."""
        await atomic_write(self.path, self.canonical)

    async def ensure_soup(self) -> BeautifulSoup:
        """Build the tree (once), canonicalising the string if needed.

        Caller must hold the session lock.  The bootstrap parse runs in a
        thread so the event loop stays responsive during the 9-12 s parse.
        Because ``str(soup)`` is not guaranteed byte-identical to the file
        produced by ``make_gui``, the first build *redefines* ``canonical``
        as ``str(soup)`` and rewrites the file — from then on string and tree
        share one serialization.
        """
        if self._soup is not None:
            self._cache._touch_soup(self)
            return self._soup
        soup = await asyncio.to_thread(BeautifulSoup, self.canonical, 'html.parser')
        canonicalized = str(soup)
        if canonicalized != self.canonical:
            self.canonical = canonicalized  # single synchronous statement
            await self.persist()
        self._soup = soup
        self._cache._touch_soup(self)
        return soup

    # -- row-scoped commits (sync: no awaits between tree change and splice) --

    def commit_row(self, row_id: str) -> str:
        """Splice the current tree state of ``row_id`` into ``canonical``.

        Returns the new row HTML.  Call ``persist()`` afterwards.
        """
        if self._soup is None:
            raise RuntimeError("commit_row requires the soup to be loaded")
        row = self._soup.find('tr', id=row_id)
        if row is None:
            raise SpliceError(f"Row '{row_id}' not found in tree")
        new_row_html = str(row)
        self.canonical = replace_row_str(self.canonical, row_id, new_row_html)
        return new_row_html

    def commit_row_removal(self, row_id: str) -> None:
        if self._soup is not None:
            row = self._soup.find('tr', id=row_id)
            if row is not None:
                row.decompose()
        self.canonical = remove_row_str(self.canonical, row_id)

    def commit_row_append(self, row_id: str) -> str:
        """Append the (already tree-appended) row at the end of the table."""
        if self._soup is None:
            raise RuntimeError("commit_row_append requires the soup to be loaded")
        row = self._soup.find('tr', id=row_id)
        if row is None:
            raise SpliceError(f"Row '{row_id}' not found in tree")
        new_row_html = str(row)
        self.canonical = insert_row_str(self.canonical, new_row_html, None)
        return new_row_html

    def row_html_or_none(self, row_id: str) -> Optional[str]:
        try:
            start, end = find_row_bounds(self.canonical, row_id)
            return self.canonical[start:end]
        except SpliceError:
            return None

    async def restore_row(self, row_id: str, row_html: Optional[str],
                          next_row_id: Optional[str]) -> None:
        """Undo/redo restore: remove / replace / re-insert one row.

        Caller must hold the session lock.  ``row_html=None`` removes the row;
        otherwise the row is replaced in place, or re-inserted before
        ``next_row_id`` (append at end when None/missing) if it is absent.
        The tree (when loaded) is synced via a 1 ms mini-parse of the row.
        """
        if row_html is None:
            try:
                self.commit_row_removal(row_id)
            except SpliceError:
                pass  # row already absent — nothing to remove
        else:
            try:
                self.canonical = replace_row_str(self.canonical, row_id, row_html)
                if self._soup is not None:
                    old = self._soup.find('tr', id=row_id)
                    if old is not None:
                        new_node = BeautifulSoup(row_html, 'html.parser').find('tr')
                        if new_node is not None:
                            old.replace_with(new_node)
            except SpliceError:
                self.canonical = insert_row_str(self.canonical, row_html, next_row_id)
                if self._soup is not None:
                    tbody = self._soup.find('table', id='table-data')
                    tbody = tbody.find('tbody') if tbody else None
                    if tbody is not None:
                        new_node = BeautifulSoup(row_html, 'html.parser').find('tr')
                        if new_node is not None:
                            anchor = (tbody.find('tr', id=next_row_id)
                                      if next_row_id else None)
                            if anchor is not None:
                                anchor.insert_before(new_node)
                            else:
                                tbody.append(new_node)
        await self.persist()


class DocumentCache:
    """Per-process cache of session documents (strings + limited soups)."""

    def __init__(self) -> None:
        # (session_id, table_type) -> SessionDocument, LRU-ordered
        self._docs: OrderedDict = OrderedDict()
        # session_id -> asyncio.Lock (mutations, undo/redo, revalidate, views)
        self._locks: dict = {}

    # -- locks ---------------------------------------------------------------

    def session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # -- document access -----------------------------------------------------

    async def get_document(self, session_id: str, table_type: str) -> Optional[SessionDocument]:
        """Return the document, loading it from disk on first access.

        Returns None when the file is missing or empty (mirrors
        ``SessionManager.load_html`` semantics).  Missing files are not
        cached negatively; a later ``update()`` inserts the document.
        """
        key = (session_id, table_type)
        doc = self._docs.get(key)
        if doc is not None:
            self._docs.move_to_end(key)
            return doc
        path = TEMP_DIR / session_id / filename_for(table_type)
        if not path.exists() or path.stat().st_size == 0:
            return None
        try:
            async with aio_open(path, 'r', encoding='utf-8', newline='') as f:
                content = await f.read()
        except Exception:
            return None
        if not content:
            return None
        doc = SessionDocument(self, session_id, table_type, content)
        self._docs[key] = doc
        self._enforce_doc_bound()
        return doc

    async def get_canonical(self, session_id: str, table_type: str) -> Optional[str]:
        doc = await self.get_document(session_id, table_type)
        return doc.canonical if doc else None

    def update(self, session_id: str, table_type: str, content: str) -> None:
        """Record externally-written content (from ``SessionManager.save_html``).

        The canonical string is replaced and the tree is dropped — it will be
        rebuilt lazily from the new canonical on the next mutation.
        """
        key = (session_id, table_type)
        doc = self._docs.get(key)
        if doc is None:
            doc = SessionDocument(self, session_id, table_type, content)
            self._docs[key] = doc
        else:
            self._docs.move_to_end(key)
            doc.canonical = content  # single synchronous statement
            doc._soup = None
        self._enforce_doc_bound()

    def drop_document(self, session_id: str, table_type: str) -> None:
        """Evict one document (tree divergence recovery, soup pressure)."""
        self._docs.pop((session_id, table_type), None)

    def drop_session(self, session_id: str) -> None:
        """Evict everything belonging to a session (draft delete, cleanup)."""
        for key in [k for k in self._docs if k[0] == session_id]:
            self._docs.pop(key, None)
        self._locks.pop(session_id, None)

    # -- internal LRU bookkeeping --------------------------------------------

    def _touch_soup(self, doc: SessionDocument) -> None:
        """Track docs holding trees; evict the least-recently used tree."""
        holders = [d for d in self._docs.values() if d._soup is not None]
        if doc not in holders:
            holders.append(doc)
        while len(holders) > MAX_CACHED_SOUPS:
            holders.pop(0)._soup = None  # string stays; tree rebuilds lazily

    def _enforce_doc_bound(self) -> None:
        while len(self._docs) > MAX_CACHED_DOCUMENTS:
            self._docs.popitem(last=False)  # drop oldest document entirely


# Module-level singleton used by SessionManager and routes
document_cache = DocumentCache()
