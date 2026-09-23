"""Service for managing session files and persistence.

Since the event-sourced-journal refactor, editing state lives in the
per-table journals ``journal_{table_type}.jsonl`` (see
``services/journal.py``); this module only handles
session identity, uploaded files, and atomic reads/writes of the HTML
artifacts (baseline = immutable base, ``*_table.html`` = commit artifacts).
"""
import json
import uuid
from pathlib import Path
from typing import Optional
from aiofiles import open as aio_open

from models import Session
from config import TEMP_DIR
from services.session_document import atomic_write, document_cache, filename_for
from services import view_builder


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
        """Save session metadata to ``session.json``."""
        session_file = TEMP_DIR / session.session_id / 'session.json'
        async with aio_open(session_file, 'w', encoding='utf-8', newline='') as f:
            await f.write(json.dumps(session.to_dict(), indent=2))

    @staticmethod
    async def load_session(session_id: str) -> Optional[Session]:
        """Load session from ``session.json``; None if not found."""
        session_file = TEMP_DIR / session_id / 'session.json'

        if not session_file.exists():
            return None

        async with aio_open(session_file, 'r', encoding='utf-8') as f:
            content = await f.read()

        return Session.from_dict(json.loads(content))

    # ---------------------------------------------------------------------------
    # HTML file IO (atomic, newline-stable)
    #
    #  table_type='meta'    → meta_table.html   (commit artifact, edited table)
    #  table_type='cits'    → cits_table.html   (commit artifact, edited table)
    #  table_type='display' → meta_html.html    (merged display; also derivable
    #                                             via session_document.compose_display)
    # ---------------------------------------------------------------------------

    @staticmethod
    async def save_html(session_id: str, html_content: str, table_type: str) -> str:
        """Atomically save an HTML artifact.  Returns the file path."""
        session_dir = TEMP_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        html_file = session_dir / filename_for(table_type)
        await atomic_write(html_file, html_content)
        return str(html_file)

    @staticmethod
    async def load_html(session_id: str, table_type: str) -> Optional[str]:
        """Load an HTML artifact, or None if missing/empty."""
        html_file = TEMP_DIR / session_id / filename_for(table_type)
        if not html_file.exists() or html_file.stat().st_size == 0:
            return None
        try:
            async with aio_open(html_file, 'r', encoding='utf-8',
                                newline='') as f:
                return await f.read()
        except Exception:
            return None

    # ---------------------------------------------------------------------------
    # Baseline snapshots (the immutable per-generation base)
    # ---------------------------------------------------------------------------

    @staticmethod
    def _baseline_filename(table_type: str) -> str:
        return f"baseline_{table_type}.html"

    @staticmethod
    async def save_baseline_snapshot(session_id: str, html_content: str, table_type: str) -> None:
        """Save the baseline (base) HTML for a validation generation.

        Callers must follow up with
        ``view_builder.build_generation_artifacts`` to rebuild the artifacts
        for the new generation.
        """
        session_dir = TEMP_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        baseline_file = session_dir / SessionManager._baseline_filename(table_type)
        await atomic_write(baseline_file, html_content)

    @staticmethod
    def list_sessions() -> list:
        """List all session IDs present under TEMP_DIR."""
        if not TEMP_DIR.exists():
            return []
        return [d.name for d in TEMP_DIR.iterdir() if d.is_dir()]

    @staticmethod
    def delete_session(session_id: str) -> bool:
        """Delete a session directory and evict all in-memory state."""
        session_dir = TEMP_DIR / session_id

        if not session_dir.exists():
            return False

        document_cache.drop_session(session_id)
        view_builder.drop_session_states(session_id)

        import shutil
        shutil.rmtree(session_dir)
        return True
