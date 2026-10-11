"""Shared table-type resolution ('meta'/'cits') for the edit/export routes.

Both tables of a paired session are editable and share one row/item id
space, so every mutation/export request carries an explicit ``table_type``
that must be validated against the session before use; ``None`` keeps the
historical default (the session's primary table).  Resolving against the
wrong table would silently edit the wrong table.
"""
from typing import Optional, Tuple

from fastapi import HTTPException

from models import Session


def editable_table_type(session: Session) -> str:
    """The session's primary table ('meta' when metadata was uploaded)."""
    return 'meta' if session.has_metadata else 'cits'


def session_table_types(session: Session) -> Tuple[str, ...]:
    """The table types present in the session, meta first (document order)."""
    tts = []
    if session.has_metadata:
        tts.append('meta')
    if session.has_citations:
        tts.append('cits')
    return tuple(tts)


def resolve_table_type(session: Session, requested: Optional[str]) -> str:
    """Validate a client-requested table type against the session.

    ``None`` keeps the historical behaviour (the session's primary table).
    Unknown types are a 422; types absent from the session are a 404.
    """
    if requested is None:
        return editable_table_type(session)
    if requested not in ('meta', 'cits'):
        raise HTTPException(status_code=422,
                            detail=f"Invalid table_type '{requested}' "
                                   f"(expected 'meta' or 'cits')")
    if requested not in session_table_types(session):
        raise HTTPException(status_code=404,
                            detail=f"Table '{requested}' not in this session")
    return requested
