"""Append-only change journal — the source of truth during editing.

Every mutation is recorded as one normalized event line in the table's
journal ``journal_{table_type}.jsonl``; the companion
``journal_{table_type}_state.json`` tracks the replay cursor (undo
position), the last written sequence number, and the last committed
(saved) sequence number.  Each table of a session ('meta' and 'cits')
has its own journal: independent undo history, cursor, saved marker and
generation stamp.

Semantics:
- **View** = base (baseline HTML + generation artifacts) + events ``[:cursor]``.
  Nothing is ever inverse-applied; undo/redo simply move the cursor.
- **Session-wide undo stack**: every event carries a ``gseq`` stamp
  (``time.time_ns()`` at append) that orders it against the events of the
  OTHER table's journal too — undo/redo move the cursor of whichever journal
  holds the chronologically last/first event, so both tables share one
  logical stack (events of pre-gseq sessions fall back to ``0``; those
  histories are single-journal, so journal order is chronological).
  A new edit **truncates the redo tails of every journal** of the session
  (``truncate_redo_tails``) — single-stream semantics: a new action destroys
  the whole redo stack.
- A new event after an undo **truncates** the journal beyond the cursor
  (atomic full rewrite — never an in-place byte truncate).
- Crash consistency: the event line is appended *before* the state file
  is atomically rewritten.  Loading tolerates a torn trailing line and clamps
  ``cursor``/``saved_seq`` to the number of valid events.
- **Generation stamp**: events are only valid against the artifacts generation
  they were created against.  ``load(expected_generation=...)`` discards
  events stamped with a different generation (e.g. a crash between revalidate
  writing new artifacts and resetting the journal).
- The journal is table-scoped: ``table_type`` records which table the
  events target and is asserted on load — both tables of a paired session
  reuse the same row/item id space, so events stamped for another table are
  never replayed against this one.  The ``gseq`` ordering never crosses that
  boundary: it only decides *which* journal's cursor moves, never replays
  events against the wrong table.
- **Legacy migration**: sessions created before per-table journals carry a
  single ``journal.jsonl`` + ``journal_meta.json`` pair; ``load`` adopts
  them once (verbatim) into the per-table files named by their stamp.

All journal operations must run while holding the session lock
(``document_cache.session_lock``).
"""
import json
import time
from pathlib import Path
from typing import Optional, Sequence

from aiofiles import open as aio_open

from config import TEMP_DIR
from services.session_document import atomic_write

LEGACY_JOURNAL_FILENAME = 'journal.jsonl'
LEGACY_STATE_FILENAME = 'journal_meta.json'


def _journal_filename(table_type: str) -> str:
    return f'journal_{table_type}.jsonl'


def _state_filename(table_type: str) -> str:
    # '_state' suffix — the legacy 'journal_meta.json' held the metadata OF
    # the journal, while this file is the state of the meta table's journal.
    return f'journal_{table_type}_state.json'


class ChangeJournal:
    """In-memory representation of one table's change journal."""

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
        return TEMP_DIR / self.session_id / _journal_filename(self.table_type)

    @property
    def _meta_path(self) -> Path:
        return TEMP_DIR / self.session_id / _state_filename(self.table_type)

    # -- loading -------------------------------------------------------------

    @classmethod
    async def _migrate_legacy_files(cls, session_id: str) -> None:
        """Adopt a pre-per-table journal (``journal.jsonl`` + ``journal_meta.json``).

        The legacy state's ``table_type`` stamp says which table the history
        belongs to.  The raw event text is copied verbatim (a torn trailing
        line stays torn for the tolerant loader) and the state is
        re-serialized under the per-table names before the legacy files are
        removed.  Events are written before the state file, mirroring
        ``append``'s crash ordering — a crash mid-migration leaves the
        legacy files in place (they stay authoritative until both new files
        exist and the legacy ones are gone), so the next load simply re-runs.
        """
        base = TEMP_DIR / session_id
        legacy_state = base / LEGACY_STATE_FILENAME
        legacy_journal = base / LEGACY_JOURNAL_FILENAME
        if not legacy_state.exists() and not legacy_journal.exists():
            return
        meta: dict = {}
        if legacy_state.exists():
            try:
                async with aio_open(legacy_state, 'r', encoding='utf-8') as f:
                    meta = json.loads(await f.read())
            except Exception:
                meta = {}
        table_type = meta.get('table_type', 'meta')
        if legacy_journal.exists():
            async with aio_open(legacy_journal, 'r', encoding='utf-8') as f:
                raw_events = await f.read()
            await atomic_write(base / _journal_filename(table_type), raw_events)
        await atomic_write(base / _state_filename(table_type),
                           json.dumps({
                               'generation': meta.get('generation'),
                               'table_type': table_type,
                               'cursor': meta.get('cursor', 0),
                               'last_seq': meta.get('last_seq', 0),
                               'saved_seq': meta.get('saved_seq', 0),
                           }, indent=2))
        legacy_state.unlink(missing_ok=True)
        legacy_journal.unlink(missing_ok=True)

    @classmethod
    async def load(cls, session_id: str, expected_generation: Optional[str] = None,
                   table_type: str = 'meta') -> 'ChangeJournal':
        """Load and reconcile the journal for one table of a session.

        ``expected_generation`` comes from the current generation artifacts;
        a mismatch (stale events from before a revalidate) resets the journal.
        """
        await cls._migrate_legacy_files(session_id)

        state_path = TEMP_DIR / session_id / _state_filename(table_type)
        journal_path = TEMP_DIR / session_id / _journal_filename(table_type)

        meta: dict = {}
        meta_exists = False
        if state_path.exists():
            try:
                async with aio_open(state_path, 'r', encoding='utf-8') as f:
                    meta = json.loads(await f.read())
                meta_exists = True
            except Exception:
                meta = {}

        events: list = []
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

        if meta_exists and stored_table_type != table_type:
            # Foreign events (a journal stamped for the other table) are
            # never replayed against this table — both tables reuse the
            # same row/item id space, so replaying would corrupt the
            # wrong table.
            if events:
                events = []
                generation = expected_generation
            stored_table_type = table_type

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
        """Append a normalized event; truncates any undone tail first.

        Every event is stamped with ``gseq`` (monotonic wall-clock ns) so
        the session-wide undo stack can order it against the other table's
        events."""
        ev = {'seq': self.cursor + 1, 'op': op, 'gseq': time.time_ns(), **fields}
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

    async def truncate_redo_tail(self) -> bool:
        """Drop the events beyond the cursor (the redo tail).  Returns True
        when anything was dropped.  Used by ``truncate_redo_tails`` — a new
        edit must destroy the whole session's redo stack, not just this
        journal's."""
        if self.cursor >= len(self.events):
            return False
        self.events = self.events[:self.cursor]
        await self._rewrite_events()
        await self._write_meta()
        return True

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


async def truncate_redo_tails(session_id: str, table_types: Sequence[str],
                              exclude: Sequence[str] = ()) -> None:
    """Drop the redo tail of every journal in the session (except ``exclude``).

    Single-stack semantics: a new edit in one table destroys the redo stack
    of the whole session — otherwise a redo could resurrect an edit older
    than a newer one.  Caller must hold the session lock."""
    for tt in table_types:
        if tt in exclude:
            continue
        journal = await ChangeJournal.load(session_id, table_type=tt)
        await journal.truncate_redo_tail()
