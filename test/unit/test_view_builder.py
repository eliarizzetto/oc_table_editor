"""Unit tests for generation artifacts + journal replay (services/view_builder.py).

A small handcrafted baseline (same markup contract as ``make_gui``) drives
the tests, so no validator run is needed here.
"""
import json

import pytest

from services.view_builder import (TableView, build_generation_artifacts,
                                   load_table_state, parse_row_fields,
                                   synthesize_row_html)

SESSION = "view-test-session"


@pytest.fixture(autouse=True)
def session_dir(temp_env):
    """Artifacts are persisted into the session dir — create it up front."""
    (temp_env / SESSION).mkdir(parents=True, exist_ok=True)

# Mirrors make_gui output: thead (# + fields), tbody rows with
# td.row-number + td.field-value {field} cells holding item-containers.
BASELINE_HTML = """<!DOCTYPE html>
<html><body>
<div class="table-container container-fluid">
<table id="table-data" class="table table-bordered table-sm">
<thead class="table-light">
<tr><th>#</th><th>id</th><th>title</th><th>author</th></tr>
</thead>
<tbody>
<tr id="row0" class="data-row valid-row"><td class="row-number">0</td>
<td class="field-value id"><span id="0-id-0" class="item-container"><span class="item-data">doi:10.1/one</span><span id="meta-0" class="issue-icon error duplicate_id"></span><span class="sep"> </span></span><span id="0-id-1" class="item-container"><span class="item-data">doi:10.1/two</span></span></td>
<td class="field-value title"><span id="0-title-0" class="item-container"><span class="item-data">First Title</span></span></td>
<td class="field-value author"><span id="0-author-0" class="item-container"><span class="item-data">Smith, J.</span></span></td>
</tr>
<tr id="row1" class="data-row valid-row"><td class="row-number">1</td>
<td class="field-value id"><span id="1-id-0" class="item-container"><span class="item-data">doi:10.1/three</span></span></td>
<td class="field-value title"><span id="1-title-0" class="item-container"><span class="item-data">Second Title</span></span></td>
<td class="field-value author"><span id="1-author-0" class="item-container"><span class="item-data">Doe, J.</span></span></td>
</tr>
</tbody>
</table>
</div>
</body></html>
"""


@pytest.fixture
async def artifacts(temp_env):
    """Persist BASELINE_HTML as the session's baseline, then build the
    generation artifacts from it (mirrors the upload flow)."""
    baseline = temp_env / SESSION / "baseline_meta.html"
    baseline.write_text(BASELINE_HTML, encoding="utf-8", newline="")
    generation = await build_generation_artifacts(SESSION, "meta")
    state = await load_table_state(SESSION, "meta")
    return state["artifacts"], generation


def make_view(artifacts, events):
    return TableView(BASELINE_HTML, artifacts, events)


# ---------------------------------------------------------------------------
# Artifact building
# ---------------------------------------------------------------------------

class TestBuildArtifacts:
    async def test_artifact_shape(self, artifacts):
        art, generation = artifacts
        assert isinstance(generation, str) and len(generation) == 36  # uuid4
        assert art["has_table"] is True
        assert art["headers"] == ["id", "title", "author"]
        assert art["row_ids"] == ["row0", "row1"]
        assert art["rows"]["row0"]["id"] == [["0-id-0", "doi:10.1/one"],
                                             ["0-id-1", "doi:10.1/two"]]
        assert art["rows"]["row1"]["title"] == [["1-title-0", "Second Title"]]
        assert art["table_open_tag"] == ('<table class="table table-bordered '
                                         'table-sm" id="table-data">')
        assert "<thead" in art["thead_html"]

    async def test_row_offsets_point_at_the_rows(self, artifacts):
        art, _ = artifacts
        for rid in ("row0", "row1"):
            start, end = art["row_offsets"][rid]
            snippet = BASELINE_HTML[start:end]
            assert snippet.startswith(f'<tr id="{rid}"')
            assert snippet.rstrip().endswith("</tr>")

    async def test_issue_index_maps_icon_ids_to_rows(self, artifacts):
        art, _ = artifacts
        assert art["issue_index"] == {"meta-0": ["row0"]}

    async def test_artifacts_are_persisted(self, temp_env, artifacts):
        art, generation = artifacts
        path = temp_env / SESSION / "artifacts_meta.json"
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["generation"] == generation

    async def test_baseline_without_table(self, temp_env):
        baseline = temp_env / SESSION / "baseline_cits.html"
        baseline.write_text("<html><body><p>no table</p></body></html>",
                            encoding="utf-8", newline="")
        generation = await build_generation_artifacts(SESSION, "cits")
        state = await load_table_state(SESSION, "cits")
        art = state["artifacts"]
        assert generation
        assert art["has_table"] is False
        assert art["row_ids"] == []
        assert art["rows"] == {}

    async def test_row_scan_mismatch_refuses_to_build(self, temp_env):
        """A row id the offset regex cannot see (non-numeric) must abort."""
        bad = BASELINE_HTML.replace('id="row1"', 'id="rowX"')
        with pytest.raises(ValueError, match="row scan mismatch"):
            await build_generation_artifacts(SESSION, "meta", base_html=bad)


class TestLoadTableState:
    async def test_missing_baseline_returns_none(self, temp_env):
        assert await load_table_state(SESSION, "cits") is None

    async def test_state_is_cached(self, temp_env, artifacts):
        first = await load_table_state(SESSION, "meta")
        second = await load_table_state(SESSION, "meta")
        assert first is second

    async def test_missing_artifacts_are_rebuilt(self, temp_env, artifacts):
        art, generation = artifacts
        (temp_env / SESSION / "artifacts_meta.json").unlink()
        # evict the in-RAM state so the rebuild path runs
        from services import view_builder
        view_builder._STATE_CACHE.clear()

        state = await load_table_state(SESSION, "meta")
        rebuilt = state["artifacts"]
        assert rebuilt["row_ids"] == art["row_ids"]   # same content…
        assert rebuilt["generation"] != generation    # …but a fresh generation
        assert (temp_env / SESSION / "artifacts_meta.json").exists()


# ---------------------------------------------------------------------------
# Row synthesis / mini-parse
# ---------------------------------------------------------------------------

class TestSynthesizeRow:
    def test_shape(self):
        html = synthesize_row_html("row5", ["id", "title"])
        assert html.startswith('<tr id="row5"')
        assert '<td class="row-number">5</td>' in html
        assert 'id="5-id-0"' in html
        assert 'id="5-title-0"' in html
        assert html.count("item-container") == 2

    def test_is_parseable_by_parse_row_fields(self):
        html = synthesize_row_html("row5", ["id", "title"])
        fields = parse_row_fields(html)
        assert fields == {"id": [("5-id-0", "")], "title": [("5-title-0", "")]}


class TestParseRowFields:
    def test_parses_items_per_field(self):
        html = ('<tr id="row2"><td class="row-number">2</td>'
                '<td class="field-value id">'
                '<span id="2-id-0" class="item-container"><span class="item-data">a</span></span>'
                '<span id="2-id-1" class="item-container"><span class="item-data">b</span></span>'
                '</td></tr>')
        assert parse_row_fields(html) == {"id": [("2-id-0", "a"),
                                                 ("2-id-1", "b")]}

    def test_input_without_row(self):
        assert parse_row_fields("<td>nothing</td>") == {}


# ---------------------------------------------------------------------------
# TableView: replay
# ---------------------------------------------------------------------------

class TestSetItem:
    def test_edited_item_tracked_and_rendered(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "set_item", "row": "row0",
                                "item": "0-title-0", "field": "title",
                                "value": "Changed"}])
        assert view.edited_item_ids == {"0-title-0"}
        assert "Changed" in view.html
        assert "First Title" not in view.html
        row_html = view.row_html("row0")
        assert 'class="item-data edited"' in row_html

    def test_untouched_rows_use_base_offsets(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "set_item", "row": "row0",
                                "item": "0-title-0", "field": "title",
                                "value": "Changed"}])
        assert view.row_html("row1") == BASELINE_HTML[
            art["row_offsets"]["row1"][0]:art["row_offsets"]["row1"][1]]

    def test_unknown_row_returns_none(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [])
        assert view.row_html("row9") is None


class TestRemoveAndClear:
    def test_removed_item_disappears_and_ghosts(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "remove_item", "row": "row0",
                                "item": "0-id-1", "field": "id"}])
        assert 'id="0-id-1"' not in view.html
        deletions = view.compute_deletions()
        assert deletions["deleted_items"] == ["0-id-1"]
        assert deletions["deleted_item_values"]["0-id-1"] == "doi:10.1/two"

    def test_edit_to_empty_counts_as_deletion(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "set_item", "row": "row0",
                                "item": "0-title-0", "field": "title",
                                "value": ""}])
        deletions = view.compute_deletions()
        assert "0-title-0" in deletions["deleted_items"]
        assert deletions["deleted_item_values"]["0-title-0"] == "First Title"

    def test_clear_cell_ghosts_all_base_items(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "clear_cell", "row": "row0",
                                "field": "id"}])
        deletions = view.compute_deletions()
        assert sorted(deletions["deleted_items"]) == ["0-id-0", "0-id-1"]
        row_html = view.row_html("row0")
        assert 'id="0-id-0"' in row_html      # one fresh empty container
        assert 'id="0-id-1"' not in row_html

    def test_row_html_with_ghosts_splices_ghost_container(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "remove_item", "row": "row0",
                                "item": "0-id-1", "field": "id"}])
        values = {"0-id-1": "doi:10.1/two"}
        html = view.row_html_with_ghosts("row0", ["0-id-1"], values)
        assert 'id="ghost-0-id-1"' in html
        assert "deleted-ghost" in html
        assert "doi:10.1/two" in html  # the ghost shows the lost value

    def test_row_html_with_ghosts_no_deletions_is_plain(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [])
        assert view.row_html_with_ghosts("row0", [], {}) == view.row_html("row0")


class TestAddItems:
    def test_append_item_tracks_and_separates(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "append_item", "row": "row1",
                                "field": "id", "value": "doi:10.1/four"}])
        assert view.added_item_ids == {"1-id-1"}
        row_html = view.row_html("row1")
        assert 'id="1-id-1"' in row_html
        assert "doi:10.1/four" in row_html
        assert 'class="item-data added"' in row_html

    def test_init_cell_rewrites_the_cell(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "init_cell", "row": "row1",
                                "field": "title", "value": "Fresh"}])
        assert view.added_item_ids == {"1-title-0"}
        row_html = view.row_html("row1")
        assert "Fresh" in row_html
        assert "Second Title" not in row_html


class TestRows:
    def test_add_row_appends_synthesized_row(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "add_row", "row": "row2"}])
        assert view.row_ids == ["row0", "row1", "row2"]
        assert view.added_row_ids == ["row2"]
        assert 'id="row2"' in view.html
        # the added <tr> carries the 'added' tracking class
        assert 'class="added"' in view.row_html("row2")
        # appended rows land before </tbody>
        assert view.html.find('id="row2"') < view.html.find("</tbody>")

    def test_delete_row_removes_from_view_html(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "delete_row", "row": "row1"}])
        assert view.row_ids == ["row0"]
        assert 'id="row1"' not in view.html

    def test_added_then_deleted_row_leaves_no_ghost(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "add_row", "row": "row2"},
                               {"op": "delete_row", "row": "row2"}])
        assert view.row_ids == ["row0", "row1"]
        assert view.display_rows() == [
            {"row_id": "row0", "ghost": False},
            {"row_id": "row1", "ghost": False}]
        assert 'id="row2"' not in view.html

    def test_row_alive_again_when_cursor_moves_back(self, artifacts):
        """Undo is cursor movement: a row whose deletion was undone (its last
        add/delete event is no longer applied) stays in the view."""
        art, _ = artifacts
        events = [{"op": "set_item", "row": "row0", "item": "0-title-0",
                   "field": "title", "value": "x"},
                  {"op": "delete_row", "row": "row1"}]
        after_delete = make_view(art, events)
        assert "row1" not in after_delete.row_ids
        # cursor moved back past the delete (journal.applied_events)
        after_undo = make_view(art, events[:1])
        assert after_undo.row_ids == ["row0", "row1"]

    def test_display_rows_ghost_flag_and_order(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "delete_row", "row": "row0"},
                               {"op": "add_row", "row": "row2"}])
        assert view.display_rows() == [
            {"row_id": "row0", "ghost": True},
            {"row_id": "row1", "ghost": False},
            {"row_id": "row2", "ghost": False}]

    def test_ghost_row_html_shape(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "delete_row", "row": "row1"}])
        html = view.ghost_row_html("row1")
        assert 'id="ghost-row1"' in html
        assert 'data-ghost-row-id="row1"' in html
        assert "deleted" in html

    def test_next_add_row_id_avoids_used_ids(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [])
        assert view.next_add_row_id() == "row2"
        # reserved rows the view cannot see (added-then-deleted, redo tails)
        assert view.next_add_row_id(reserved_rows={"row7"}) == "row8"

    def test_next_add_row_id_counts_live_added_rows(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "add_row", "row": "row2"}])
        assert view.next_add_row_id() == "row3"


class TestDerivedData:
    def test_rows_for_export_reflects_events(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [
            {"op": "set_item", "row": "row0", "item": "0-title-0",
             "field": "title", "value": "Exported"},
            {"op": "remove_item", "row": "row0", "item": "0-id-1",
             "field": "id"},
            {"op": "delete_row", "row": "row1"},
            {"op": "add_row", "row": "row2"},
        ])
        rows = view.rows_for_export()
        assert [r["title"] for r in rows] == [["Exported"], [""]]
        assert rows[0]["id"] == ["doi:10.1/one"]
        assert rows[1]["title"] == [""]

    def test_search_row_ids_is_case_insensitive(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [])
        assert view.search_row_ids("SECOND TITLE") == {"row1"}
        assert view.search_row_ids("doi:10.1/one") == {"row0"}

    def test_search_finds_edited_values_not_removed_items(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [
            {"op": "set_item", "row": "row0", "item": "0-title-0",
             "field": "title", "value": "Needle"},
            {"op": "remove_item", "row": "row1", "item": "1-id-0",
             "field": "id"},
        ])
        assert view.search_row_ids("needle") == {"row0"}
        assert "row1" not in view.search_row_ids("doi:10.1/three")

    def test_search_includes_ghost_rows(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "delete_row", "row": "row1"}])
        assert view.search_row_ids("Second Title") == {"row1"}

    def test_changed_row_ids(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [
            {"op": "set_item", "row": "row0", "item": "0-title-0",
             "field": "title", "value": "x"},
            {"op": "delete_row", "row": "row1"},
            {"op": "add_row", "row": "row2"},
        ])
        assert view.changed_row_ids() == {"row0", "row1", "row2"}

    def test_issue_row_ids(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [])
        assert view.issue_row_ids() == {"row0"}

    def test_compute_deletions_skips_added_rows(self, artifacts):
        art, _ = artifacts
        view = make_view(art, [{"op": "add_row", "row": "row2"},
                               {"op": "set_item", "row": "row2",
                                "item": "2-id-0", "field": "id",
                                "value": "x"},
                               {"op": "remove_item", "row": "row2",
                                "item": "2-id-0", "field": "id"}])
        assert view.compute_deletions()["deleted_items"] == []
