"""Service for managing session files and persistence."""
import os
import json
import uuid
from pathlib import Path
from typing import Dict, Optional
from aiofiles import open as aio_open

from models import Session, EditState, RowChangeState, DeletedItemState
from config import TEMP_DIR
from services.session_document import (
    SessionDocument,
    atomic_write,
    document_cache,
    filename_for,
    next_row_id_in_str,
)


class SessionManager:
    """Manage session storage and persistence."""
    
    @staticmethod
    def create_session_id() -> str:
        """Generate a unique session ID."""
        return str(uuid.uuid4())
    
    @staticmethod
    def create_session_dir(session_id: str) -> Path:
        """
        Create a directory for the session.
        
        Args:
            session_id: Unique session identifier
            
        Returns:
            Path to the created session directory
        """
        session_dir = TEMP_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir
    
    @staticmethod
    async def save_uploaded_file(session_id: str, file_content: bytes, filename: str) -> str:
        """
        Save an uploaded CSV file to session directory.
        
        Args:
            session_id: Session identifier
            file_content: File content as bytes
            filename: Original filename
            
        Returns:
            Path to saved file
        """
        session_dir = TEMP_DIR / session_id
        file_path = session_dir / filename
        
        async with aio_open(file_path, 'wb') as f:
            await f.write(file_content)
        
        return str(file_path)
    
    @staticmethod
    async def save_session(session: Session) -> None:
        """
        Save session metadata to JSON file.
        
        Args:
            session: Session object to save
        """
        session_dir = TEMP_DIR / session.session_id
        session_file = session_dir / 'session.json'
        
        async with aio_open(session_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(session.to_dict(), indent=2))
    
    @staticmethod
    async def load_session(session_id: str) -> Optional[Session]:
        """
        Load session from JSON file.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Session object or None if not found
        """
        session_file = TEMP_DIR / session_id / 'session.json'
        
        if not session_file.exists():
            return None
        
        async with aio_open(session_file, 'r', encoding='utf-8') as f:
            content = await f.read()
        
        return Session.from_dict(json.loads(content))
    
    @staticmethod
    async def save_edit_state(session_id: str, edit_states: Dict[str, EditState]) -> None:
        """
        Save edit tracking state to JSON file.
        
        Args:
            session_id: Session identifier
            edit_states: Dictionary of item_id -> EditState
        """
        session_dir = TEMP_DIR / session_id
        state_file = session_dir / 'edit_state.json'
        
        # Convert EditState objects to dicts
        state_dict = {
            item_id: state.to_dict() 
            for item_id, state in edit_states.items()
        }
        
        async with aio_open(state_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(state_dict, indent=2))
    
    @staticmethod
    async def load_edit_state(session_id: str) -> Dict[str, EditState]:
        """
        Load edit tracking state from JSON file.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary of item_id -> EditState
        """
        state_file = TEMP_DIR / session_id / 'edit_state.json'
        
        if not state_file.exists():
            return {}
        
        async with aio_open(state_file, 'r', encoding='utf-8') as f:
            content = await f.read()
        
        state_dict = json.loads(content)
        
        # Convert dicts back to EditState objects
        return {
            item_id: EditState.from_dict(state_data)
            for item_id, state_data in state_dict.items()
        }
    
    # ---------------------------------------------------------------------------
    # HTML file-name scheme
    #
    #  table_type='meta'    → meta_table.html   (individual meta table only)
    #  table_type='cits'    → cits_table.html   (individual cits table only)
    #  table_type='display' → meta_html.html    (the file served to the browser;
    #                                             for paired sessions this file
    #                                             is written at upload/revalidate
    #                                             for compatibility, but the
    #                                             served view is *derived* from
    #                                             the two canonical tables — see
    #                                             session_document.compose_display)
    #
    # Reads/writes go through the in-memory document cache
    # (services/session_document.py) so canonical strings stay hot and parsed
    # trees are shared across requests.  All HTML writes are atomic
    # (temp file + os.replace) and newline-stable.
    # ---------------------------------------------------------------------------

    @staticmethod
    def _html_filename(table_type: str) -> str:
        """Return the on-disk filename for a given table_type key."""
        return filename_for(table_type)

    @staticmethod
    async def save_html(session_id: str, html_content: str, table_type: str) -> str:
        """
        Save HTML content to file (atomically) and update the document cache.

        Args:
            session_id:   Session identifier.
            html_content: HTML string to save.
            table_type:   'meta', 'cits', or 'display'.

        Returns:
            Path to saved HTML file.
        """
        session_dir = TEMP_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        html_file = session_dir / SessionManager._html_filename(table_type)

        await atomic_write(html_file, html_content)
        document_cache.update(session_id, table_type, html_content)

        return str(html_file)

    @staticmethod
    async def load_html(session_id: str, table_type: str) -> Optional[str]:
        """
        Load HTML content (from the document cache / disk).

        Args:
            session_id: Session identifier.
            table_type: 'meta', 'cits', or 'display'.

        Returns:
            HTML content as string or None if not found.
        """
        return await document_cache.get_canonical(session_id, table_type)
    
    @staticmethod
    async def load_report(session_id: str, table_type: str) -> Optional[dict]:
        """
        Load validation report from JSON file.
        
        Args:
            session_id: Session identifier
            table_type: 'meta' or 'cits'
            
        Returns:
            Report as dictionary or None if not found
        """
        session = await SessionManager.load_session(session_id)
        if session is None:
            return None
        
        report_path = session.meta_report_path if table_type == 'meta' else session.cits_report_path
        if report_path is None or not Path(report_path).exists():
            return None
        
        async with aio_open(report_path, 'r', encoding='utf-8') as f:
            content = await f.read()
        
        return json.loads(content)
    
    # ---------------------------------------------------------------------------
    # Baseline snapshot management (for deletion detection)
    # ---------------------------------------------------------------------------
    
    @staticmethod
    def _baseline_filename(table_type: str) -> str:
        """Return the on-disk filename for baseline snapshots."""
        return f"baseline_{table_type}.html"
    
    @staticmethod
    async def save_baseline_snapshot(session_id: str, html_content: str, table_type: str) -> None:
        """
        Save the baseline HTML state after validation for diff comparison.

        This baseline is used to identify deleted items and rows by comparing
        the current HTML state with this saved baseline.  Cached under the
        ``baseline_{table_type}`` document key.

        Args:
            session_id: Session identifier
            html_content: HTML content to save as baseline
            table_type: 'meta' or 'cits'
        """
        session_dir = TEMP_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        baseline_file = session_dir / SessionManager._baseline_filename(table_type)

        await atomic_write(baseline_file, html_content)
        document_cache.update(session_id, f'baseline_{table_type}', html_content)

    @staticmethod
    async def load_baseline_snapshot(session_id: str, table_type: str) -> Optional[str]:
        """
        Load the baseline HTML state for a session.

        Args:
            session_id: Session identifier
            table_type: 'meta' or 'cits'

        Returns:
            Baseline HTML content as string or None if not found
        """
        return await document_cache.get_canonical(session_id, f'baseline_{table_type}')
    
    # ---------------------------------------------------------------------------
    # Row change state management (added/deleted row tracking)
    # ---------------------------------------------------------------------------
    
    @staticmethod
    async def save_row_change_state(
        session_id: str, 
        row_changes: Dict[str, RowChangeState]
    ) -> None:
        """
        Save row-level change tracking state to JSON file.
        
        Args:
            session_id: Session identifier
            row_changes: Dictionary of row_id -> RowChangeState
        """
        session_dir = TEMP_DIR / session_id
        state_file = session_dir / 'row_change_state.json'
        
        # Convert RowChangeState objects to dicts
        state_dict = {
            row_id: state.to_dict() 
            for row_id, state in row_changes.items()
        }
        
        async with aio_open(state_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(state_dict, indent=2))
    
    @staticmethod
    async def load_row_change_state(
        session_id: str
    ) -> Dict[str, RowChangeState]:
        """
        Load row-level change tracking state from JSON file.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary of row_id -> RowChangeState
        """
        state_file = TEMP_DIR / session_id / 'row_change_state.json'
        
        if not state_file.exists():
            return {}
        
        try:
            async with aio_open(state_file, 'r', encoding='utf-8') as f:
                content = await f.read()
            
            state_dict = json.loads(content)
            
            # Convert dicts back to RowChangeState objects
            return {
                row_id: RowChangeState.from_dict(state_data)
                for row_id, state_data in state_dict.items()
            }
        except Exception:
            return {}
    
    # ---------------------------------------------------------------------------
    # Deleted item state management (for ghost overlays)
    # ---------------------------------------------------------------------------
    
    @staticmethod
    async def save_deleted_item_state(
        session_id: str, 
        deleted_items: Dict[str, DeletedItemState]
    ) -> None:
        """
        Save deleted item state to JSON file.
        
        Args:
            session_id: Session identifier
            deleted_items: Dictionary of item_id -> DeletedItemState
        """
        session_dir = TEMP_DIR / session_id
        state_file = session_dir / 'deleted_item_state.json'
        
        # Convert DeletedItemState objects to dicts
        state_dict = {
            item_id: state.to_dict() 
            for item_id, state in deleted_items.items()
        }
        
        async with aio_open(state_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(state_dict, indent=2))
    
    @staticmethod
    async def load_deleted_item_state(
        session_id: str
    ) -> Dict[str, DeletedItemState]:
        """
        Load deleted item state from JSON file.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary of item_id -> DeletedItemState
        """
        state_file = TEMP_DIR / session_id / 'deleted_item_state.json'
        
        if not state_file.exists():
            return {}
        
        try:
            async with aio_open(state_file, 'r', encoding='utf-8') as f:
                content = await f.read()
            
            state_dict = json.loads(content)
            
            # Convert dicts back to DeletedItemState objects
            return {
                item_id: DeletedItemState.from_dict(state_data)
                for item_id, state_data in state_dict.items()
            }
        except Exception:
            return {}
    
    # ---------------------------------------------------------------------------
    # Undo / Redo snapshot management (format v2 — row-level entries)
    #
    # Every mutation is row-scoped, so undo entries store the affected row's
    # pre-mutation HTML (~2 KB) instead of a full-document snapshot (12 MB).
    # An entry is a single JSON file ``undo/{table_type}_{idx}.row.json``:
    #
    #   {
    #     "row_id": "row5",                # affected row
    #     "pre_row_html": "<tr ...>…",     # None for add_row (row was absent)
    #     "next_row_id": "row6",           # re-insertion anchor (None = append)
    #     "edit_state": {...},             # pre-mutation tracking sidecars
    #     "row_change_state": {...},
    #     "deleted_item_state": {...}
    #   }
    #
    # Legacy (v1) full-document stacks from sessions created before this
    # format are discarded on first touch (undo history is ephemeral; data
    # and edits are unaffected).
    #
    # All undo/redo entry manipulation must run while holding the session
    # lock (``document_cache.session_lock``).
    # ---------------------------------------------------------------------------

    MAX_UNDO_DEPTH: int = 20
    UNDO_VERSION: int = 2

    @staticmethod
    def _undo_dir(session_id: str) -> Path:
        """Return path to the undo-snapshots subdirectory for a session."""
        return TEMP_DIR / session_id / 'undo'

    @staticmethod
    async def load_undo_state(session_id: str) -> dict:
        """Load undo/redo index from ``undo_state.json``.  Returns ``{}`` on miss."""
        state_file = TEMP_DIR / session_id / 'undo_state.json'
        if not state_file.exists():
            return {}
        try:
            async with aio_open(state_file, 'r', encoding='utf-8') as f:
                return json.loads(await f.read())
        except Exception:
            return {}

    @staticmethod
    async def save_undo_state(session_id: str, state: dict) -> None:
        state_file = TEMP_DIR / session_id / 'undo_state.json'
        async with aio_open(state_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(state, indent=2))

    @staticmethod
    async def _ensure_undo_v2(session_id: str, state: dict) -> dict:
        """Discard legacy v1 undo history on first touch of a session."""
        if state.get('v') == SessionManager.UNDO_VERSION:
            return state
        undo_dir = SessionManager._undo_dir(session_id)
        if undo_dir.exists():
            for f in undo_dir.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)
        return {'v': SessionManager.UNDO_VERSION}

    @staticmethod
    async def _capture_tracking_sidecars(session_id: str) -> dict:
        """Snapshot the three tracking-state dicts for an undo/redo entry."""
        edit_states = await SessionManager.load_edit_state(session_id)
        row_changes = await SessionManager.load_row_change_state(session_id)
        deleted_items = await SessionManager.load_deleted_item_state(session_id)
        return {
            'edit_state': {k: s.to_dict() for k, s in edit_states.items()},
            'row_change_state': {k: s.to_dict() for k, s in row_changes.items()},
            'deleted_item_state': {k: s.to_dict() for k, s in deleted_items.items()},
        }

    @staticmethod
    async def _restore_tracking_sidecars(session_id: str, entry: dict) -> None:
        """Restore tracking-state dicts from an undo/redo entry."""
        edit_dict = entry.get('edit_state') or {}
        await SessionManager.save_edit_state(session_id, {
            k: EditState.from_dict(v) for k, v in edit_dict.items()
        })
        row_dict = entry.get('row_change_state') or {}
        await SessionManager.save_row_change_state(session_id, {
            k: RowChangeState.from_dict(v) for k, v in row_dict.items()
        })
        del_dict = entry.get('deleted_item_state') or {}
        await SessionManager.save_deleted_item_state(session_id, {
            k: DeletedItemState.from_dict(v) for k, v in del_dict.items()
        })

    @staticmethod
    async def _write_undo_entry(session_id: str, table_type: str,
                                entry: dict) -> int:
        """Write one .row.json entry file and return its index."""
        undo_dir = SessionManager._undo_dir(session_id)
        undo_dir.mkdir(parents=True, exist_ok=True)
        state = await SessionManager.load_undo_state(session_id)
        state = await SessionManager._ensure_undo_v2(session_id, state)
        ts = state.get(table_type, {'undo': [], 'redo': []})
        undo_stack: list = ts.get('undo', [])
        new_idx = (max(undo_stack) + 1) if undo_stack else 0
        entry_path = undo_dir / f"{table_type}_{new_idx}.row.json"
        async with aio_open(entry_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(entry, indent=2))
        return new_idx

    @staticmethod
    async def push_undo_row_snapshot(
        session_id: str, table_type: str, row_id: str,
        pre_row_html: Optional[str], next_row_id: Optional[str] = None
    ) -> None:
        """
        Push a row-level undo entry (must be called BEFORE the mutation).

        Clears the redo stack (forward history is lost when a new edit is
        made), then stores the row's pre-mutation HTML plus the pre-mutation
        tracking sidecars (edit_state, row_change_state, deleted_item_state).
        """
        undo_dir = SessionManager._undo_dir(session_id)
        undo_dir.mkdir(parents=True, exist_ok=True)

        state = await SessionManager.load_undo_state(session_id)
        state = await SessionManager._ensure_undo_v2(session_id, state)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        # Clear redo entries
        for idx in ts.get('redo', []):
            (undo_dir / f"{table_type}_{idx}.row.json").unlink(missing_ok=True)
        ts['redo'] = []

        undo_stack: list = ts.get('undo', [])
        new_idx = (max(undo_stack) + 1) if undo_stack else 0

        entry = {
            'row_id': row_id,
            'pre_row_html': pre_row_html,
            'next_row_id': next_row_id,
            **await SessionManager._capture_tracking_sidecars(session_id),
        }
        entry_path = undo_dir / f"{table_type}_{new_idx}.row.json"
        async with aio_open(entry_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(entry, indent=2))

        undo_stack.append(new_idx)
        while len(undo_stack) > SessionManager.MAX_UNDO_DEPTH:
            oldest = undo_stack.pop(0)
            (undo_dir / f"{table_type}_{oldest}.row.json").unlink(missing_ok=True)

        ts['undo'] = undo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)

    @staticmethod
    async def pop_undo_row_snapshot(
        session_id: str, table_type: str, doc: SessionDocument
    ) -> Optional[dict]:
        """
        Undo: restore the most recent row-level entry.

        Captures the current (post-mutation) row image and tracking sidecars
        onto the redo stack, then restores the entry's row (via
        ``doc.restore_row``) and its sidecars.

        Returns the restored entry, or None when there is nothing to undo.
        """
        undo_dir = SessionManager._undo_dir(session_id)
        state = await SessionManager.load_undo_state(session_id)
        state = await SessionManager._ensure_undo_v2(session_id, state)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        undo_stack: list = ts.get('undo', [])
        if not undo_stack:
            return None

        prev_idx = undo_stack.pop()
        entry_path = undo_dir / f"{table_type}_{prev_idx}.row.json"
        if not entry_path.exists():
            ts['undo'] = undo_stack
            state[table_type] = ts
            await SessionManager.save_undo_state(session_id, state)
            return None

        async with aio_open(entry_path, 'r', encoding='utf-8') as f:
            entry = json.loads(await f.read())

        # Capture the post-mutation image for redo
        row_id = entry.get('row_id')
        post_row_html = doc.row_html_or_none(row_id)
        post_next_row_id = (next_row_id_in_str(doc.canonical, row_id)
                            if post_row_html is not None else None)
        redo_entry = {
            'row_id': row_id,
            'pre_row_html': post_row_html,
            'next_row_id': post_next_row_id,
            **await SessionManager._capture_tracking_sidecars(session_id),
        }
        redo_stack: list = ts.get('redo', [])
        all_existing = undo_stack + redo_stack
        redo_idx = (max(all_existing) + 1) if all_existing else 0
        redo_path = undo_dir / f"{table_type}_{redo_idx}.row.json"
        async with aio_open(redo_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(redo_entry, indent=2))
        redo_stack.append(redo_idx)

        # Restore the row and the pre-mutation tracking sidecars
        await doc.restore_row(row_id, entry.get('pre_row_html'),
                              entry.get('next_row_id'))
        await SessionManager._restore_tracking_sidecars(session_id, entry)

        ts['undo'] = undo_stack
        ts['redo'] = redo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)
        return entry

    @staticmethod
    async def pop_redo_row_snapshot(
        session_id: str, table_type: str, doc: SessionDocument
    ) -> Optional[dict]:
        """
        Redo: re-apply the most recently undone row mutation.

        Pushes the current (pre-redo) row image and sidecars back onto the
        undo stack, then restores the redo entry's row and sidecars.

        Returns the restored entry, or None when there is nothing to redo.
        """
        undo_dir = SessionManager._undo_dir(session_id)
        state = await SessionManager.load_undo_state(session_id)
        state = await SessionManager._ensure_undo_v2(session_id, state)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        redo_stack: list = ts.get('redo', [])
        if not redo_stack:
            return None

        next_idx = redo_stack.pop()
        entry_path = undo_dir / f"{table_type}_{next_idx}.row.json"
        if not entry_path.exists():
            ts['redo'] = redo_stack
            state[table_type] = ts
            await SessionManager.save_undo_state(session_id, state)
            return None

        async with aio_open(entry_path, 'r', encoding='utf-8') as f:
            entry = json.loads(await f.read())

        # Capture the pre-redo image for the undo stack
        row_id = entry.get('row_id')
        pre_row_html = doc.row_html_or_none(row_id)
        pre_next_row_id = (next_row_id_in_str(doc.canonical, row_id)
                           if pre_row_html is not None else None)
        undo_entry = {
            'row_id': row_id,
            'pre_row_html': pre_row_html,
            'next_row_id': pre_next_row_id,
            **await SessionManager._capture_tracking_sidecars(session_id),
        }
        undo_stack: list = ts.get('undo', [])
        all_existing = undo_stack + redo_stack
        undo_idx = (max(all_existing) + 1) if all_existing else 0
        undo_path = undo_dir / f"{table_type}_{undo_idx}.row.json"
        async with aio_open(undo_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(undo_entry, indent=2))
        undo_stack.append(undo_idx)

        # Restore the row and the post-mutation tracking sidecars
        await doc.restore_row(row_id, entry.get('pre_row_html'),
                              entry.get('next_row_id'))
        await SessionManager._restore_tracking_sidecars(session_id, entry)

        ts['undo'] = undo_stack
        ts['redo'] = redo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)
        return entry

    @staticmethod
    async def get_undo_availability(session_id: str, table_type: str) -> dict:
        """Return ``{"can_undo": bool, "can_redo": bool}`` for the given table."""
        state = await SessionManager.load_undo_state(session_id)
        state = await SessionManager._ensure_undo_v2(session_id, state)
        ts = state.get(table_type, {})
        return {
            'can_undo': len(ts.get('undo', [])) > 0,
            'can_redo': len(ts.get('redo', [])) > 0,
        }

    @staticmethod
    def list_sessions() -> list:
        """
        List all available session IDs.
        
        Returns:
            List of session IDs
        """
        if not TEMP_DIR.exists():
            return []
        
        return [d.name for d in TEMP_DIR.iterdir() if d.is_dir()]
    
    @staticmethod
    def delete_session(session_id: str) -> bool:
        """
        Delete a session directory, all its files, and its cached documents.

        Args:
            session_id: Session identifier

        Returns:
            True if deleted, False if not found
        """
        session_dir = TEMP_DIR / session_id

        if not session_dir.exists():
            return False

        # Evict in-memory state first so no stale document outlives the dir
        document_cache.drop_session(session_id)

        import shutil
        shutil.rmtree(session_dir)
        return True