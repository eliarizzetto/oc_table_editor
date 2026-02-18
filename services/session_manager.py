"""Service for managing session files and persistence."""
import os
import json
import uuid
from pathlib import Path
from typing import Dict, Optional
from aiofiles import open as aio_open

from models import Session, EditState
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