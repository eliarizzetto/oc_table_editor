"""Unit tests for session persistence (services/session_manager.py)."""
import json

import pytest

from models import Session
from services.session_manager import SessionManager

SESSION = "sm-test-session"


async def make_session_dir(temp_env, session_id=SESSION):
    SessionManager.create_session_dir(session_id)
    return temp_env / session_id


class TestSessionPersistence:
    async def test_save_and_load_round_trip(self, temp_env):
        await make_session_dir(temp_env)
        session = Session(session_id=SESSION, has_metadata=True,
                          draft_name="Round trip", meta_csv_path="x.csv")
        await SessionManager.save_session(session)

        loaded = await SessionManager.load_session(SESSION)
        assert loaded is not None
        assert loaded.session_id == SESSION
        assert loaded.has_metadata is True
        assert loaded.draft_name == "Round trip"
        assert loaded.meta_csv_path == "x.csv"

    async def test_load_missing_session_returns_none(self, temp_env):
        assert await SessionManager.load_session("no-such-session") is None

    async def test_session_json_is_valid_json(self, temp_env):
        await make_session_dir(temp_env)
        await SessionManager.save_session(Session(session_id=SESSION))
        raw = json.loads((temp_env / SESSION / "session.json").read_text(
            encoding="utf-8"))
        assert raw["session_id"] == SESSION


class TestSaveUploadedFile:
    async def test_saves_under_original_filename(self, temp_env):
        await make_session_dir(temp_env)
        path = await SessionManager.save_uploaded_file(
            SESSION, b"id,title\n", "my data.csv", "fallback.csv")
        assert path.endswith("my data.csv")
        assert (temp_env / SESSION / "my data.csv").read_bytes() == b"id,title\n"

    @pytest.mark.parametrize("unsafe,expected_name", [
        ("..\\..\\evil.csv", "evil.csv"),   # traversal via parent dirs
        ("../../evil.csv", "evil.csv"),
        ("C:\\abs\\path\\x.csv", "x.csv"),
        ("/abs/path/x.csv", "x.csv"),
    ])
    async def test_paths_are_sanitized_to_final_component(self, temp_env,
                                                          unsafe, expected_name):
        await make_session_dir(temp_env)
        path = await SessionManager.save_uploaded_file(
            SESSION, b"x", unsafe, "fallback.csv")
        # the file lands inside the session dir under its final component
        assert path == str(temp_env / SESSION / expected_name)
        assert (temp_env / SESSION / expected_name).exists()
        # nothing escaped the session directory
        assert not (temp_env / expected_name).exists()

    @pytest.mark.parametrize("empty", ["", ".", ".."])
    async def test_empty_names_fall_back(self, temp_env, empty):
        await make_session_dir(temp_env)
        path = await SessionManager.save_uploaded_file(
            SESSION, b"x", empty, "metadata.csv")
        assert path.endswith("metadata.csv")

    async def test_nul_bytes_are_stripped(self, temp_env):
        await make_session_dir(temp_env)
        await SessionManager.save_uploaded_file(
            SESSION, b"x", "a\x00b.csv", "fallback.csv")
        assert (temp_env / SESSION / "ab.csv").exists()


class TestHtmlArtifacts:
    async def test_save_html_writes_commit_artifacts(self, temp_env):
        await make_session_dir(temp_env)
        await SessionManager.save_html(SESSION, "<table>meta</table>", "meta")
        await SessionManager.save_html(SESSION, "<table>cits</table>", "cits")
        assert (temp_env / SESSION / "meta_table.html").read_text(
            encoding="utf-8") == "<table>meta</table>"
        assert (temp_env / SESSION / "cits_table.html").read_text(
            encoding="utf-8") == "<table>cits</table>"

    async def test_save_html_rejects_unknown_type(self, temp_env):
        await make_session_dir(temp_env)
        with pytest.raises(ValueError):
            await SessionManager.save_html(SESSION, "x", "bibliography")

    async def test_save_baseline_snapshot(self, temp_env):
        await make_session_dir(temp_env)
        await SessionManager.save_baseline_snapshot(SESSION, "<tr>base</tr>",
                                                    "meta")
        assert (temp_env / SESSION / "baseline_meta.html").read_text(
            encoding="utf-8") == "<tr>base</tr>"


class TestListAndDelete:
    async def test_list_sessions_empty(self, temp_env):
        assert SessionManager.list_sessions() == []

    async def test_list_sessions_finds_directories(self, temp_env):
        for sid in ("aaa", "bbb"):
            await make_session_dir(temp_env, sid)
        assert sorted(SessionManager.list_sessions()) == ["aaa", "bbb"]

    async def test_delete_session_removes_directory(self, temp_env):
        await make_session_dir(temp_env)
        (temp_env / SESSION / "leftover.json").write_text("{}", encoding="utf-8")
        assert SessionManager.delete_session(SESSION) is True
        assert not (temp_env / SESSION).exists()

    async def test_delete_missing_session_returns_false(self, temp_env):
        assert SessionManager.delete_session("no-such-session") is False


class TestSessionIds:
    def test_create_session_id_is_a_uuid(self):
        sid = SessionManager.create_session_id()
        assert len(sid) == 36 and sid.count("-") == 4

    def test_create_session_ids_are_unique(self):
        ids = {SessionManager.create_session_id() for _ in range(50)}
        assert len(ids) == 50
