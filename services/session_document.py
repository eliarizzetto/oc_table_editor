"""Session locks and the atomic-write/file-name primitives.

After the event-sourced-journal refactor this module no longer caches parsed
trees — the baseline HTML is immutable and views are computed by
``services/view_builder.py`` from string splices.  What remains here:

- ``atomic_write`` — temp-file + ``os.replace`` with ``newline=''`` so saved
  strings are byte-stable (used for every file the app writes);
- ``filename_for`` — the single source of truth for the on-disk HTML
  artifact names (commit tables + baselines);
- ``DocumentCache`` — the per-session ``asyncio.Lock`` factory.  Any journal
  or view read/write must hold ``document_cache.session_lock(session_id)``.
"""
import asyncio
from pathlib import Path

from aiofiles import open as aio_open
from aiofiles.os import replace as aio_os_replace


# ---------------------------------------------------------------------------
# File-name scheme (single source of truth; SessionManager delegates here)
# ---------------------------------------------------------------------------

_HTML_FILENAMES: dict = {
    'meta': 'meta_table.html',
    'cits': 'cits_table.html',
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
# Per-session locks
# ---------------------------------------------------------------------------

class DocumentCache:
    """Per-session ``asyncio.Lock`` registry.

    Every journal operation and view computation (mutations, undo/redo,
    table views, commit, revalidate, export) must hold
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
