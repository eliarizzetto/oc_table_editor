"""Draft management routes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

from services import SessionManager

router = APIRouter()


class SaveDraftRequest(BaseModel):
    """Request model for saving a draft."""
    session_id: str
    draft_name: Optional[str] = None


class LoadDraftRequest(BaseModel):
    """Request model for loading a draft."""
    session_id: str


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
        session.draft_name = request.draft_name
    
    # Save session (this already persists all files)
    await SessionManager.save_session(session)
    
    return {
        "success": True,
        "message": "Draft saved successfully",
        "session_id": request.session_id
    }


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
        "draft_name": getattr(session, 'draft_name', None),
        "last_updated": session.last_updated
    }


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
                "draft_name": getattr(session, 'draft_name', None),
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