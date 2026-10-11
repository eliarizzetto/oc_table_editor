"""Unit tests for services/table_scope.py (table-type resolution)."""
import pytest
from fastapi import HTTPException

from models import Session
from services.table_scope import (editable_table_type, resolve_table_type,
                                  session_table_types)


def meta_only_session() -> Session:
    return Session(session_id="s", has_metadata=True, has_citations=False)


def cits_only_session() -> Session:
    return Session(session_id="s", has_metadata=False, has_citations=True)


def paired_session() -> Session:
    return Session(session_id="s", has_metadata=True, has_citations=True)


class TestEditableTableType:
    def test_meta_is_primary_when_metadata_uploaded(self):
        assert editable_table_type(meta_only_session()) == "meta"
        assert editable_table_type(paired_session()) == "meta"

    def test_cits_when_only_citations_uploaded(self):
        assert editable_table_type(cits_only_session()) == "cits"


class TestSessionTableTypes:
    def test_document_order_meta_first(self):
        assert session_table_types(paired_session()) == ("meta", "cits")

    def test_single_table_sessions(self):
        assert session_table_types(meta_only_session()) == ("meta",)
        assert session_table_types(cits_only_session()) == ("cits",)


class TestResolveTableType:
    def test_none_resolves_to_primary_table(self):
        assert resolve_table_type(meta_only_session(), None) == "meta"
        assert resolve_table_type(cits_only_session(), None) == "cits"

    def test_explicit_type_resolved(self):
        assert resolve_table_type(paired_session(), "meta") == "meta"
        assert resolve_table_type(paired_session(), "cits") == "cits"

    def test_invalid_type_is_422(self):
        with pytest.raises(HTTPException) as exc:
            resolve_table_type(meta_only_session(), "bibliography")
        assert exc.value.status_code == 422

    def test_type_absent_from_session_is_404(self):
        with pytest.raises(HTTPException) as exc:
            resolve_table_type(meta_only_session(), "cits")
        assert exc.value.status_code == 404
