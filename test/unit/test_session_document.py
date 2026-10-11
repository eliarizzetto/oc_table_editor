"""Unit tests for services/session_document.py (filenames, atomic writes, locks)."""
import asyncio

import pytest

from services.session_document import DocumentCache, atomic_write, filename_for


class TestFilenameFor:
    def test_commit_artifact_names(self):
        assert filename_for("meta") == "meta_table.html"
        assert filename_for("cits") == "cits_table.html"

    def test_baseline_names(self):
        assert filename_for("baseline_meta") == "baseline_meta.html"
        assert filename_for("baseline_cits") == "baseline_cits.html"

    def test_unknown_table_type_raises(self):
        with pytest.raises(ValueError, match="Unknown table_type"):
            filename_for("bibliography")


class TestAtomicWrite:
    async def test_writes_exact_content(self, tmp_path):
        target = tmp_path / "out.txt"
        await atomic_write(target, "héllo\nworld\n")
        assert target.read_text(encoding="utf-8") == "héllo\nworld\n"

    async def test_no_crlf_translation(self, tmp_path):
        """``newline=''`` must keep LF bytes LF (no Windows \\r\\n)."""
        target = tmp_path / "out.html"
        await atomic_write(target, "<tr>\n<td>x</td>\n</tr>\n")
        assert b"\r\n" not in target.read_bytes()

    async def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old", encoding="utf-8")
        await atomic_write(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    async def test_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "out.txt"
        await atomic_write(target, "data")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


class TestDocumentCache:
    def test_same_session_gets_same_lock(self):
        cache = DocumentCache()
        assert cache.session_lock("s1") is cache.session_lock("s1")

    def test_different_sessions_get_different_locks(self):
        cache = DocumentCache()
        assert cache.session_lock("s1") is not cache.session_lock("s2")

    def test_drop_session_evicts_the_lock(self):
        cache = DocumentCache()
        lock = cache.session_lock("s1")
        cache.drop_session("s1")
        assert cache.session_lock("s1") is not lock

    def test_drop_unknown_session_is_a_noop(self):
        cache = DocumentCache()
        cache.drop_session("never-seen")

    def test_lock_is_an_asyncio_lock(self):
        cache = DocumentCache()
        assert isinstance(cache.session_lock("s1"), asyncio.Lock)
