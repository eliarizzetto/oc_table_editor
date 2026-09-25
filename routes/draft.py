"""Draft management routes."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from services import SessionManager
from services.session_document import document_cache

router = APIRouter()

MAX_DRAFT_NAME_LEN = 120


def _clean_name(raw: Optional[str]) -> Optional[str]:
    """Stripped session name (``None`` when empty); 400 when too long."""
    name = (raw or '').strip()
    if len(name) > MAX_DRAFT_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Session name must be at most {MAX_DRAFT_NAME_LEN} characters")
    return name or None


class SaveDraftRequest(BaseModel):
    """Request model for saving a draft."""
    session_id: str
    draft_name: Optional[str] = None


class LoadDraftRequest(BaseModel):
    """Request model for loading a draft."""
    session_id: str


class RenameRequest(BaseModel):
    """Request model for renaming a session."""
    session_id: str
    draft_name: str


@router.post("/save")
async def save_draft(request: SaveDraftRequest):
    """
    Save current session as a draft.
    
    - **session_id**: Session identifier
    - **draft_name**: Optional custom name for the draft
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Update session with draft name if provided
    if request.draft_name:
        session.draft_name = _clean_name(request.draft_name)

    # Save session (this already persists all files)
    await SessionManager.save_session(session)

    return {
        "success": True,
        "message": "Draft saved successfully",
        "session_id": request.session_id
    }


@router.post("/rename")
async def rename_draft(request: RenameRequest):
    """
    Rename a session (its display name in the drafts list and editor).

    - **session_id**: Session identifier
    - **draft_name**: New non-empty name (≤ 120 characters)
    """
    session = await SessionManager.load_session(request.session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    name = _clean_name(request.draft_name)
    if not name:
        raise HTTPException(status_code=400, detail="Session name cannot be empty")

    # session.json is rewritten by edit operations under the session lock;
    # renaming must hold it too to avoid losing a concurrent update.
    async with document_cache.session_lock(request.session_id):
        session.draft_name = name
        session.update_timestamp()
        await SessionManager.save_session(session)

    return {"success": True, "session_id": request.session_id, "draft_name": name}


@router.post("/load")
async def load_draft(request: LoadDraftRequest):
    """
    Load a saved draft.
    
    - **session_id**: Session identifier of the draft to load
    """
    session = await SessionManager.load_session(request.session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    return {
        "success": True,
        "session_id": session.session_id,
        "has_metadata": session.has_metadata,
        "has_citations": session.has_citations,
        "draft_name": session.draft_name,
        "last_updated": session.last_updated
    }


def _display_name(session) -> str:
    """Fallback label for unnamed sessions: the uploaded CSV's filename."""
    for p in (session.meta_csv_path, session.cits_csv_path):
        if p:
            return Path(p).stem
    return 'Untitled Draft'


@router.get("/list")
async def list_drafts():
    """
    List all available drafts.

    Returns a list of session IDs for all saved drafts.
    """
    session_ids = SessionManager.list_sessions()

    # Load session info for each
    drafts = []
    for session_id in session_ids:
        session = await SessionManager.load_session(session_id)
        if session:
            drafts.append({
                "session_id": session.session_id,
                "draft_name": session.draft_name,
                "display_name": session.draft_name or _display_name(session),
                "has_metadata": session.has_metadata,
                "has_citations": session.has_citations,
                "created_at": session.created_at,
                "last_updated": session.last_updated
            })
    
    return {"drafts": drafts}


@router.delete("/{session_id}")
async def delete_draft(session_id: str):
    """
    Delete a draft.
    
    - **session_id**: Session identifier of the draft to delete
    """
    deleted = SessionManager.delete_session(session_id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    return {
        "success": True,
        "message": f"Draft {session_id} deleted successfully"
    }