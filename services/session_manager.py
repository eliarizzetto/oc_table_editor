"""Service for managing session files and persistence."""
import os
import json
import uuid
from pathlib import Path
from typing import Dict, Optional
from aiofiles import open as aio_open

from models import Session, EditState, RowChangeState, DeletedItemState
from config import TEMP_DIR


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
    #                                             for single-table sessions this
    #                                             is the same as the individual
    #                                             file; for paired sessions it is
    #                                             the merged view)
    # ---------------------------------------------------------------------------

    _HTML_FILENAMES: dict = {
        'meta': 'meta_table.html',
        'cits': 'cits_table.html',
        'display': 'meta_html.html',
    }

    @staticmethod
    def _html_filename(table_type: str) -> str:
        """Return the on-disk filename for a given table_type key."""
        fname = SessionManager._HTML_FILENAMES.get(table_type)
        if fname is None:
            raise ValueError(
                f"Unknown table_type '{table_type}'. "
                f"Expected one of: {list(SessionManager._HTML_FILENAMES.keys())}"
            )
        return fname

    @staticmethod
    async def save_html(session_id: str, html_content: str, table_type: str) -> str:
        """
        Save HTML content to file.

        Args:
            session_id:   Session identifier.
            html_content: HTML string to save.
            table_type:   'meta', 'cits', or 'display'.

        Returns:
            Path to saved HTML file.
        """
        session_dir = TEMP_DIR / session_id
        html_file = session_dir / SessionManager._html_filename(table_type)

        async with aio_open(html_file, 'w', encoding='utf-8') as f:
            await f.write(html_content)

        return str(html_file)

    @staticmethod
    async def load_html(session_id: str, table_type: str) -> Optional[str]:
        """
        Load HTML content from file.

        Args:
            session_id: Session identifier.
            table_type: 'meta', 'cits', or 'display'.

        Returns:
            HTML content as string or None if not found.
        """
        html_file = TEMP_DIR / session_id / SessionManager._html_filename(table_type)

        if not html_file.exists():
            return None

        if html_file.stat().st_size == 0:
            return None

        try:
            async with aio_open(html_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                return content
        except Exception:
            return None
    
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
        the current HTML state with this saved baseline.
        
        Args:
            session_id: Session identifier
            html_content: HTML content to save as baseline
            table_type: 'meta' or 'cits'
        """
        session_dir = TEMP_DIR / session_id
        baseline_file = session_dir / SessionManager._baseline_filename(table_type)
        
        async with aio_open(baseline_file, 'w', encoding='utf-8') as f:
            await f.write(html_content)
    
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
        baseline_file = TEMP_DIR / session_id / SessionManager._baseline_filename(table_type)
        
        if not baseline_file.exists():
            return None
        
        if baseline_file.stat().st_size == 0:
            return None
        
        try:
            async with aio_open(baseline_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                return content
        except Exception:
            return None
    
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
    # Undo / Redo snapshot management
    # ---------------------------------------------------------------------------

    MAX_UNDO_DEPTH: int = 20

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
    async def push_undo_snapshot(
        session_id: str, html_content: str, table_type: str
    ) -> None:
        """
        Push ``html_content`` onto the undo stack for ``table_type``.

        Must be called **before** applying a mutation so that undo restores this
        exact pre-mutation state.  Clears the redo stack (forward history is lost
        when a new edit is made).
        
        Also saves edit_state and row_change_state to fully restore tracking state.
        """
        undo_dir = SessionManager._undo_dir(session_id)
        undo_dir.mkdir(parents=True, exist_ok=True)

        state = await SessionManager.load_undo_state(session_id)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        # Clear redo snapshots
        for idx in ts.get('redo', []):
            (undo_dir / f"{table_type}_{idx}.html").unlink(missing_ok=True)
            (undo_dir / f"{table_type}_{idx}_edit_state.json").unlink(missing_ok=True)
            (undo_dir / f"{table_type}_{idx}_row_change_state.json").unlink(missing_ok=True)
        ts['redo'] = []

        undo_stack: list = ts.get('undo', [])

        # Choose a monotonically increasing index
        new_idx = (max(undo_stack) + 1) if undo_stack else 0
        
        # Save HTML snapshot
        snapshot_path = undo_dir / f"{table_type}_{new_idx}.html"
        async with aio_open(snapshot_path, 'w', encoding='utf-8') as f:
            await f.write(html_content)
        
        # Save edit_state snapshot
        edit_states = await SessionManager.load_edit_state(session_id)
        edit_state_path = undo_dir / f"{table_type}_{new_idx}_edit_state.json"
        edit_state_dict = {
            item_id: state.to_dict() 
            for item_id, state in edit_states.items()
        }
        async with aio_open(edit_state_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(edit_state_dict, indent=2))
        
        # Save row_change_state snapshot
        row_change_states = await SessionManager.load_row_change_state(session_id)
        row_change_state_path = undo_dir / f"{table_type}_{new_idx}_row_change_state.json"
        row_change_state_dict = {
            row_id: state.to_dict() 
            for row_id, state in row_change_states.items()
        }
        async with aio_open(row_change_state_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(row_change_state_dict, indent=2))

        undo_stack.append(new_idx)

        # Enforce maximum depth — remove oldest entries
        while len(undo_stack) > SessionManager.MAX_UNDO_DEPTH:
            oldest = undo_stack.pop(0)
            (undo_dir / f"{table_type}_{oldest}.html").unlink(missing_ok=True)
            (undo_dir / f"{table_type}_{oldest}_edit_state.json").unlink(missing_ok=True)
            (undo_dir / f"{table_type}_{oldest}_row_change_state.json").unlink(missing_ok=True)

        ts['undo'] = undo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)

    @staticmethod
    async def pop_undo_snapshot(
        session_id: str, current_html: str, table_type: str
    ):
        """
        Undo: restore to previous snapshot.

        Pushes ``current_html`` onto the redo stack so the action can be
        redone.
        
        Also restores edit_state and row_change_state from the snapshot.

        Returns ``(previous_html, undo_state_dict)`` or ``(None, state)`` if
        there is nothing to undo.
        """
        undo_dir = SessionManager._undo_dir(session_id)
        state = await SessionManager.load_undo_state(session_id)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        undo_stack: list = ts.get('undo', [])
        if not undo_stack:
            return None, state

        # Pop most-recent undo snapshot
        prev_idx = undo_stack.pop()
        snapshot_path = undo_dir / f"{table_type}_{prev_idx}.html"
        if not snapshot_path.exists():
            ts['undo'] = undo_stack
            state[table_type] = ts
            await SessionManager.save_undo_state(session_id, state)
            return None, state

        async with aio_open(snapshot_path, 'r', encoding='utf-8') as f:
            prev_html = await f.read()

        # Restore edit_state from snapshot
        edit_state_path = undo_dir / f"{table_type}_{prev_idx}_edit_state.json"
        if edit_state_path.exists():
            async with aio_open(edit_state_path, 'r', encoding='utf-8') as f:
                edit_state_dict = json.loads(await f.read())
            
            # Convert dicts back to EditState objects
            edit_states = {
                item_id: EditState.from_dict(state_data)
                for item_id, state_data in edit_state_dict.items()
            }
            await SessionManager.save_edit_state(session_id, edit_states)

        # Restore row_change_state from snapshot
        row_change_state_path = undo_dir / f"{table_type}_{prev_idx}_row_change_state.json"
        if row_change_state_path.exists():
            async with aio_open(row_change_state_path, 'r', encoding='utf-8') as f:
                row_change_state_dict = json.loads(await f.read())
            
            # Convert dicts back to RowChangeState objects
            row_change_states = {
                row_id: RowChangeState.from_dict(state_data)
                for row_id, state_data in row_change_state_dict.items()
            }
            await SessionManager.save_row_change_state(session_id, row_change_states)

        # Save current HTML onto redo stack
        redo_stack: list = ts.get('redo', [])
        all_existing = undo_stack + redo_stack
        redo_idx = (max(all_existing) + 1) if all_existing else 0
        async with aio_open(undo_dir / f"{table_type}_{redo_idx}.html",
                            'w', encoding='utf-8') as f:
            await f.write(current_html)
        redo_stack.append(redo_idx)

        ts['undo'] = undo_stack
        ts['redo'] = redo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)

        return prev_html, state

    @staticmethod
    async def pop_redo_snapshot(
        session_id: str, current_html: str, table_type: str
    ):
        """
        Redo: restore to next snapshot.

        Pushes ``current_html`` back onto the undo stack.
        
        Also restores edit_state and row_change_state from the snapshot.

        Returns ``(next_html, undo_state_dict)`` or ``(None, state)`` if there
        is nothing to redo.
        """
        undo_dir = SessionManager._undo_dir(session_id)
        state = await SessionManager.load_undo_state(session_id)
        ts = state.get(table_type, {'undo': [], 'redo': []})

        redo_stack: list = ts.get('redo', [])
        if not redo_stack:
            return None, state

        # Pop most-recent redo snapshot
        next_idx = redo_stack.pop()
        snapshot_path = undo_dir / f"{table_type}_{next_idx}.html"
        if not snapshot_path.exists():
            ts['redo'] = redo_stack
            state[table_type] = ts
            await SessionManager.save_undo_state(session_id, state)
            return None, state

        async with aio_open(snapshot_path, 'r', encoding='utf-8') as f:
            next_html = await f.read()

        # Restore edit_state from snapshot
        edit_state_path = undo_dir / f"{table_type}_{next_idx}_edit_state.json"
        if edit_state_path.exists():
            async with aio_open(edit_state_path, 'r', encoding='utf-8') as f:
                edit_state_dict = json.loads(await f.read())
            
            # Convert dicts back to EditState objects
            edit_states = {
                item_id: EditState.from_dict(state_data)
                for item_id, state_data in edit_state_dict.items()
            }
            await SessionManager.save_edit_state(session_id, edit_states)

        # Restore row_change_state from snapshot
        row_change_state_path = undo_dir / f"{table_type}_{next_idx}_row_change_state.json"
        if row_change_state_path.exists():
            async with aio_open(row_change_state_path, 'r', encoding='utf-8') as f:
                row_change_state_dict = json.loads(await f.read())
            
            # Convert dicts back to RowChangeState objects
            row_change_states = {
                row_id: RowChangeState.from_dict(state_data)
                for row_id, state_data in row_change_state_dict.items()
            }
            await SessionManager.save_row_change_state(session_id, row_change_states)

        # Push current HTML back onto undo stack
        undo_stack: list = ts.get('undo', [])
        all_existing = undo_stack + redo_stack
        undo_idx = (max(all_existing) + 1) if all_existing else 0
        async with aio_open(undo_dir / f"{table_type}_{undo_idx}.html",
                            'w', encoding='utf-8') as f:
            await f.write(current_html)
        undo_stack.append(undo_idx)

        ts['undo'] = undo_stack
        ts['redo'] = redo_stack
        state[table_type] = ts
        await SessionManager.save_undo_state(session_id, state)

        return next_html, state

    @staticmethod
    async def get_undo_availability(session_id: str, table_type: str) -> dict:
        """Return ``{"can_undo": bool, "can_redo": bool}`` for the given table."""
        state = await SessionManager.load_undo_state(session_id)
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
        Delete a session directory and all its files.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if deleted, False if not found
        """
        session_dir = TEMP_DIR / session_id
        
        if not session_dir.exists():
            return False
        
        import shutil
        shutil.rmtree(session_dir)
        return True