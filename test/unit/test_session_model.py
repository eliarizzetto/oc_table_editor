"""Unit tests for the Session dataclass (models/session.py)."""
from models import Session


def make_session(**overrides) -> Session:
    params = dict(
        session_id="s1",
        has_metadata=True,
        has_citations=False,
        verify_id_existence=False,
        meta_csv_path="temp/s1/meta.csv",
        meta_report_path="temp/s1/out_validate_meta.jsonl",
    )
    params.update(overrides)
    return Session(**params)


class TestSerialization:
    def test_to_dict_from_dict_round_trip(self):
        session = make_session(draft_name="My draft")
        restored = Session.from_dict(session.to_dict())
        assert restored.session_id == "s1"
        assert restored.has_metadata is True
        assert restored.has_citations is False
        assert restored.draft_name == "My draft"
        assert restored.meta_csv_path == "temp/s1/meta.csv"
        assert restored.meta_report_path == "temp/s1/out_validate_meta.jsonl"
        assert restored.created_at == session.created_at
        assert restored.last_updated == session.last_updated

    def test_from_dict_defaults_for_missing_keys(self):
        restored = Session.from_dict({"session_id": "s2"})
        assert restored.session_id == "s2"
        assert restored.has_metadata is False
        assert restored.has_citations is False
        assert restored.verify_id_existence is False
        assert restored.draft_name is None
        assert restored.meta_csv_path is None
        assert restored.cits_report_path is None
        assert restored.last_validated_at is None
        assert restored.has_edits_since_validation is False


class TestStateTransitions:
    def test_mark_edited_sets_flag_and_touches_timestamp(self):
        session = make_session()
        before = session.last_updated
        session.mark_edited()
        assert session.has_edits_since_validation is True
        assert session.last_updated >= before

    def test_mark_validated_clears_flag_and_stamps_validation_time(self):
        session = make_session()
        session.mark_edited()
        session.mark_validated()
        assert session.has_edits_since_validation is False
        assert session.last_validated_at is not None
        assert session.last_updated >= session.created_at

    def test_update_timestamp(self):
        session = make_session()
        old = session.last_updated
        session.update_timestamp()
        assert session.last_updated >= old
