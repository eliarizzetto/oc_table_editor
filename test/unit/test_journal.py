"""Unit tests for the append-only change journal (services/journal.py).

Covers persistence, cursor semantics (undo/redo), crash consistency
(torn trailing line, clamped state), generation/table-type stamps, and the
session-wide redo-tail truncation.
"""
import json

import pytest

from services.journal import ChangeJournal, truncate_redo_tails

SESSION = "journal-test-session"


@pytest.fixture(autouse=True)
def session_dir(temp_env):
    """The journal writes into the session dir — create it up front."""
    (temp_env / SESSION).mkdir(parents=True, exist_ok=True)


def journal_file(temp_env, table_type="meta"):
    return temp_env / SESSION / f"journal_{table_type}.jsonl"


def state_file(temp_env, table_type="meta"):
    return temp_env / SESSION / f"journal_{table_type}_state.json"


def read_state(temp_env, table_type="meta"):
    return json.loads(state_file(temp_env, table_type).read_text(encoding="utf-8"))


class TestLoadEmpty:
    async def test_load_without_files_gives_empty_journal(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        assert journal.events == []
        assert journal.cursor == 0
        assert journal.saved_seq == 0
        assert journal.can_undo is False
        assert journal.can_redo is False
        assert journal.has_unsaved_changes is False

    async def test_load_with_unrelated_state_only(self, temp_env):
        """A state file for the OTHER table never feeds this journal."""
        (temp_env / SESSION).mkdir(parents=True, exist_ok=True)
        state_file(temp_env, "cits").write_text(
            json.dumps({"generation": "g", "table_type": "cits",
                        "cursor": 9, "saved_seq": 9}),
            encoding="utf-8")
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        assert journal.events == []
        assert journal.cursor == 0


class TestAppend:
    async def test_append_persists_event_and_state(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        ev = await journal.append("set_item", row="row0", item="0-title-0",
                                  field="title", value="New")
        assert ev["op"] == "set_item"
        assert ev["row"] == "row0"
        assert isinstance(ev["gseq"], int)

        lines = journal_file(temp_env).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["op"] == "set_item"
        state = read_state(temp_env)
        assert state["cursor"] == 1
        assert state["table_type"] == "meta"

    async def test_gseq_stamps_are_monotonic(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        first = await journal.append("set_item", row="row0", value="a")
        second = await journal.append("set_item", row="row0", value="b")
        assert second["gseq"] > first["gseq"]

    async def test_cursor_tracks_appends(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        for i in range(3):
            await journal.append("set_item", row=f"row{i}", value=str(i))
        assert journal.cursor == 3
        assert len(journal.events) == 3
        assert journal.can_undo is True
        assert journal.can_redo is False
        assert journal.has_unsaved_changes is True  # nothing saved yet


class TestUndoRedo:
    async def test_undo_moves_cursor_back(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.append("set_item", row="row1", value="2")

        ev = await journal.undo()
        assert ev["row"] == "row1"
        assert journal.cursor == 1
        assert journal.applied_events == journal.events[:1]
        assert journal.can_redo is True

    async def test_undo_on_empty_journal_returns_none(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        assert await journal.undo() is None

    async def test_redo_reapplies_event(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.undo()
        ev = await journal.redo()
        assert ev["op"] == "set_item"
        assert journal.cursor == 1

    async def test_redo_at_tail_returns_none(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        assert await journal.redo() is None

    async def test_append_after_undo_truncates_tail_on_disk(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.append("set_item", row="row1", value="2")
        await journal.undo()  # drop event 2

        await journal.append("set_item", row="row2", value="3")
        assert journal.cursor == 2
        assert len(journal.events) == 2
        assert journal.events[-1]["row"] == "row2"
        assert journal.can_redo is False

        lines = journal_file(temp_env).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # the undone event is gone from disk too
        assert json.loads(lines[-1])["row"] == "row2"


class TestSavedMarker:
    async def test_mark_saved_clears_unsaved_flag(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        assert journal.has_unsaved_changes is True

        await journal.mark_saved()
        assert journal.has_unsaved_changes is False
        assert read_state(temp_env)["saved_seq"] == 1

    async def test_undo_below_save_keeps_unsaved_flag(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.mark_saved()
        await journal.undo()
        # saved_seq > cursor is legitimate: the file on disk holds more
        # than the view, but the view no longer matches the save.
        assert journal.has_unsaved_changes is True


class TestCrashConsistency:
    async def test_torn_trailing_line_is_ignored(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.append("set_item", row="row1", value="2")

        # Simulate a crash mid-append: a partial JSON line at the tail.
        with journal_file(temp_env).open("a", encoding="utf-8") as f:
            f.write('{"op": "set_it')

        reloaded = await ChangeJournal.load(SESSION, table_type="meta")
        assert len(reloaded.events) == 2
        assert reloaded.cursor == 2

    async def test_cursor_and_saved_seq_are_clamped_to_valid_events(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.append("set_item", row="row1", value="2")

        # A stale state file referencing more events than exist on disk.
        state = read_state(temp_env)
        state["cursor"] = 99
        state["saved_seq"] = 99
        state_file(temp_env).write_text(json.dumps(state), encoding="utf-8")

        reloaded = await ChangeJournal.load(SESSION, table_type="meta")
        assert reloaded.cursor == 2
        assert reloaded.saved_seq == 2

    async def test_event_without_required_keys_stops_the_load(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        # A line that parses as JSON but is not a valid event dict.
        with journal_file(temp_env).open("a", encoding="utf-8") as f:
            f.write('{"gseq": 1}\n')
        reloaded = await ChangeJournal.load(SESSION, table_type="meta")
        assert len(reloaded.events) == 1


class TestGenerationStamp:
    async def test_events_from_a_stale_generation_are_discarded(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.append("set_item", row="row0", value="1")

        # Revalidate wrote new artifacts with a new generation.
        reloaded = await ChangeJournal.load(SESSION,
                                            expected_generation="gen-2")
        assert reloaded.events == []
        assert reloaded.cursor == 0
        assert reloaded.generation == "gen-2"

    async def test_matching_generation_keeps_events(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.append("set_item", row="row0", value="1")

        reloaded = await ChangeJournal.load(SESSION,
                                            expected_generation="gen-1")
        assert len(reloaded.events) == 1
        assert reloaded.cursor == 1


class TestTableTypeStamp:
    async def test_foreign_table_events_are_never_replayed(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.append("set_item", row="row0", value="1")

        # Corrupt the stamp: the state file claims these events target the
        # citations table — loading for 'meta' must refuse to replay them.
        state = read_state(temp_env)
        state["table_type"] = "cits"
        state_file(temp_env).write_text(json.dumps(state), encoding="utf-8")

        reloaded = await ChangeJournal.load(SESSION, expected_generation="gen-1",
                                            table_type="meta")
        assert reloaded.events == []
        assert reloaded.table_type == "meta"


class TestEnsureInitialized:
    async def test_fresh_session_writes_empty_files(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        assert journal.events == []
        assert read_state(temp_env)["generation"] == "gen-1"
        assert journal_file(temp_env).exists()

    async def test_existing_empty_journal_is_restamped(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.ensure_initialized("gen-2", "meta")

        reloaded = await ChangeJournal.load(SESSION, table_type="meta")
        assert reloaded.generation == "gen-2"
        assert reloaded.events == []

    async def test_journal_with_events_is_reset(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.append("set_item", row="row0", value="1")

        await journal.ensure_initialized("gen-2", "meta")
        assert journal.events == []
        assert journal.cursor == 0


class TestReset:
    async def test_reset_clears_everything_and_restamps(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.ensure_initialized("gen-1", "meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.mark_saved()

        await journal.reset("gen-2", "meta")
        assert journal.events == []
        assert journal.cursor == 0
        assert journal.saved_seq == 0
        assert journal.generation == "gen-2"

        reloaded = await ChangeJournal.load(SESSION, table_type="meta")
        assert reloaded.events == []
        assert reloaded.saved_seq == 0


class TestTruncateRedoTails:
    async def test_truncate_redo_tail_drops_undone_events(self, temp_env):
        journal = await ChangeJournal.load(SESSION, table_type="meta")
        await journal.append("set_item", row="row0", value="1")
        await journal.append("set_item", row="row1", value="2")
        await journal.undo()

        assert await journal.truncate_redo_tail() is True
        assert len(journal.events) == 1
        assert await journal.truncate_redo_tail() is False  # nothing left

    async def test_truncate_redo_tails_hits_every_journal(self, temp_env):
        """Single-stack semantics: a new edit destroys the redo stack of the
        whole session, not just the edited table's journal."""
        meta = await ChangeJournal.load(SESSION, table_type="meta")
        cits = await ChangeJournal.load(SESSION, table_type="cits")
        for journal, row in ((meta, "row0"), (cits, "row5")):
            await journal.append("set_item", row=row, value="1")
            await journal.append("set_item", row=row, value="2")
            await journal.undo()

        await truncate_redo_tails(SESSION, ("meta", "cits"))

        for tt in ("meta", "cits"):
            reloaded = await ChangeJournal.load(SESSION, table_type=tt)
            assert len(reloaded.events) == 1
            assert reloaded.can_redo is False

    async def test_exclude_protects_a_journal(self, temp_env):
        meta = await ChangeJournal.load(SESSION, table_type="meta")
        cits = await ChangeJournal.load(SESSION, table_type="cits")
        for journal, row in ((meta, "row0"), (cits, "row5")):
            await journal.append("set_item", row=row, value="1")
            await journal.append("set_item", row=row, value="2")
            await journal.undo()

        await truncate_redo_tails(SESSION, ("meta", "cits"), exclude=("meta",))

        meta = await ChangeJournal.load(SESSION, table_type="meta")
        cits = await ChangeJournal.load(SESSION, table_type="cits")
        assert meta.can_redo is True   # excluded: tail preserved
        assert cits.can_redo is False  # truncated
