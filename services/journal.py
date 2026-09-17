"""Append-only change journal — the source of truth during editing.

Every mutation is recorded as one normalized event line in ``journal.jsonl``;
``journal_meta.json`` tracks the replay cursor (undo position), the last
written sequence number, and the last committed (saved) sequence number.

Semantics:
- **View** = base (baseline HTML + generation artifacts) + events ``[:cursor]``.
  Nothing is ever inverse-applied; undo/redo simply move the cursor.
- A new event after an undo **truncates** the journal beyond the cursor
  (atomic full rewrite — never an in-place byte truncate).
- Crash consistency: the event line is appended *before* ``journal_meta.json``
  is atomically rewritten.  Loading tolerates a torn trailing line and clamps
  ``cursor``/``saved_seq`` to the number of valid events.
- **Generation stamp**: events are only valid against the artifacts generation
  they were created against.  ``load(expected_generation=...)`` discards
  events stamped with a different generation (e.g. a crash between revalidate
  writing new artifacts and resetting the journal).
- The journal is table-scoped: ``table_type`` records which table the events
  target ('meta' for paired sessions — citations are not editable today) and
  is asserted on load.

All journal operations must run while holding the session lock
(``document_cache.session_lock``).
"""
import json
from pathlib import Path
from typing import Optional

from aiofiles import open as aio_open

from config import TEMP_DIR
from services.session_document import atomic_write

JOURNAL_FILENAME = 'journal.jsonl'
META_FILENAME = 'journal_meta.json'


class ChangeJournal:
    """In-memory representation of a session's change journal."""

    def __init__(self, session_id: str, events: list, generation: str,
                 table_type: str, cursor: int, saved_seq: int,
                 meta_exists: bool):
        self.session_id = session_id
        self.events = events            # ordered list of event dicts (1-based seq == index+1)
        self.generation = generation
        self.table_type = table_type
        self.cursor = cursor            # number of events applied to the view
        self.saved_seq = saved_seq      # cursor value at the last commit
        self._meta_exists = meta_exists

    # -- paths ---------------------------------------------------------------

    @property
    def _journal_path(self) -> Path:
        return TEMP_DIR / self.session_id / JOURNAL_FILENAME

    @property
    def _meta_path(self) -> Path:
        return TEMP_DIR / self.session_id / META_FILENAME

    # -- loading -------------------------------------------------------------

    @classmethod
    async def load(cls, session_id: str, expected_generation: Optional[str] = None,
                   table_type: str = 'meta') -> 'ChangeJournal':
        """Load and reconcile the journal for a session.

        ``expected_generation`` comes from the current generation artifacts;
        a mismatch (stale events from before a revalidate) resets the journal.
        """
        meta: dict = {}
        meta_exists = False
        if (TEMP_DIR / session_id / META_FILENAME).exists():
            try:
                async with aio_open(TEMP_DIR / session_id / META_FILENAME,
                                    'r', encoding='utf-8') as f:
                    meta = json.loads(await f.read())
                meta_exists = True
            except Exception:
                meta = {}

        events: list = []
        journal_path = TEMP_DIR / session_id / JOURNAL_FILENAME
        if journal_path.exists():
            async with aio_open(journal_path, 'r', encoding='utf-8') as f:
                content = await f.read()
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    break  # torn/partial trailing line — ignore the tail
                if not isinstance(ev, dict) or 'op' not in ev or 'row' not in ev:
                    break
                events.append(ev)

        last_seq = len(events)
        generation = meta.get('generation')
        stored_table_type = meta.get('table_type', table_type)

        if meta_exists and expected_generation is not None and generation != expected_generation:
            # Events belong to a previous generation (pre-revalidate) — discard.
            events = []
            generation = expected_generation

        cursor = min(meta.get('cursor', 0), last_seq) if events or meta_exists else 0
        if not events:
            cursor = 0
        # Clamp saved_seq to existing events only — saved_seq > cursor is a
        # legitimate state (undo below the last save; the file on disk holds
        # more than the view and no rewrite happens on undo).
        saved_seq = min(meta.get('saved_seq', 0), last_seq)

        return cls(session_id, events, generation or expected_generation or '',
                   stored_table_type, cursor, saved_seq, meta_exists)

    async def ensure_initialized(self, generation: str, table_type: str) -> None:
        """Create the journal files for a fresh session (upload)."""
        if self._meta_exists and self.events == [] and self.cursor == 0:
            # Reuse the (empty) loaded journal but stamp the fresh generation.
            self.generation = generation
            self.table_type = table_type
            await self._write_meta()
            return
        await self.reset(generation, table_type)

    # -- persistence ----------------------------------------------------------

    async def _write_meta(self) -> None:
        payload = json.dumps({
            'generation': self.generation,
            'table_type': self.table_type,
            'cursor': self.cursor,
            'last_seq': len(self.events),
            'saved_seq': self.saved_seq,
        }, indent=2)
        await atomic_write(self._meta_path, payload)
        self._meta_exists = True

    async def _rewrite_events(self) -> None:
        payload = ''.join(json.dumps(ev) + '\n' for ev in self.events)
        await atomic_write(self._journal_path, payload)

    # -- operations (caller holds the session lock) ---------------------------

    async def append(self, op: str, **fields) -> dict:
        """Append a normalized event; truncates any undone tail first."""
        ev = {'seq': self.cursor + 1, 'op': op, **fields}
        if self.cursor < len(self.events):
            self.events = self.events[:self.cursor]
            self.events.append(ev)
            await self._rewrite_events()
        else:
            self.events.append(ev)
            async with aio_open(self._journal_path, 'a', encoding='utf-8',
                                newline='') as f:
                await f.write(json.dumps(ev) + '\n')
        self.cursor = len(self.events)
        await self._write_meta()
        return ev

    async def undo(self) -> Optional[dict]:
        """Move the cursor back one event.  Returns the event being undone."""
        if self.cursor == 0:
            return None
        self.cursor -= 1
        await self._write_meta()
        return self.events[self.cursor]

    async def redo(self) -> Optional[dict]:
        """Move the cursor forward one event.  Returns the re-applied event."""
        if self.cursor >= len(self.events):
            return None
        ev = self.events[self.cursor]
        self.cursor += 1
        await self._write_meta()
        return ev

    async def mark_saved(self) -> None:
        """Commit marker: the on-disk table file now reflects ``cursor``."""
        self.saved_seq = self.cursor
        await self._write_meta()

    async def reset(self, generation: str, table_type: str) -> None:
        """Clear the journal against a new generation (after revalidate)."""
        self.events = []
        self.cursor = 0
        self.saved_seq = 0
        self.generation = generation
        self.table_type = table_type
        await self._rewrite_events()
        await self._write_meta()

    # -- derived state ----------------------------------------------------------

    @property
    def applied_events(self) -> list:
        return self.events[:self.cursor]

    @property
    def can_undo(self) -> bool:
        return self.cursor > 0

    @property
    def can_redo(self) -> bool:
        return self.cursor < len(self.events)

    @property
    def has_unsaved_changes(self) -> bool:
        return self.cursor != self.saved_seq
