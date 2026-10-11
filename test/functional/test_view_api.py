"""Functional tests for the table view endpoint (GET /api/edit/table/…):
filters, facets, search, pagination and the stats header."""
import re

from test.support import make_meta_csv

# META_MIXED_CSV: rows 0-1 have one error each; row 2 ('Good Row') is clean.


def view(client, sid, **params):
    resp = client.get(f"/api/edit/table/{sid}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def row_ids_in(html):
    """Live (non-ghost) row ids present in a rendered fragment."""
    return set(re.findall(r'<tr[^>]*\sid="(row\d+)"', html))


class TestDefaultView:
    def test_default_view_shows_issue_rows_only(self, client,
                                                mixed_meta_session):
        body = view(client, mixed_meta_session)
        assert body["table_type"] == "meta"
        assert body["cits"] is None          # solo meta session
        assert row_ids_in(body["html"]) == {"row0", "row1"}

    def test_show_all_reveals_clean_rows(self, client, mixed_meta_session):
        html = view(client, mixed_meta_session, show_all="true")["html"]
        assert row_ids_in(html) == {"row0", "row1", "row2"}

    def test_valid_table_default_view_is_empty_with_cta(self, client,
                                                        valid_meta_session):
        body = view(client, valid_meta_session)
        html = body["html"]
        assert row_ids_in(html) == set()
        assert "empty-state" in html
        assert 'data-empty-show-all' in html   # the "Show all rows" CTA
        assert "No issues found" in html       # ✓ reassurance line

    def test_valid_table_is_still_editable(self, client, valid_meta_session):
        html = view(client, valid_meta_session, show_all="true")["html"]
        assert row_ids_in(html) == {"row0", "row1"}
        assert 'data-editable="1"' in html


class TestChangesOnly:
    def test_changes_only_empty_before_edits(self, client,
                                             mixed_meta_session):
        html = view(client, mixed_meta_session, changes_only="true")["html"]
        assert row_ids_in(html) == set()
        assert "empty-state" in html

    def test_changes_only_after_edit(self, client, mixed_meta_session):
        sid = mixed_meta_session
        client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "2-title-0",
            "new_value": "Touched"})
        html = view(client, sid, changes_only="true")["html"]
        assert row_ids_in(html) == {"row2"}

    def test_changes_only_includes_ghost_rows(self, client,
                                              mixed_meta_session):
        sid = mixed_meta_session
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row2"})
        html = view(client, sid, changes_only="true")["html"]
        assert 'data-ghost-row-id="row2"' in html


class TestSearch:
    def test_search_finds_clean_rows(self, client, mixed_meta_session):
        body = view(client, mixed_meta_session, q="Good Row")
        assert row_ids_in(body["html"]) == {"row2"}
        assert "1 row matching" in body["html"]  # match-count badge

    def test_search_is_case_insensitive_and_substring(self, client,
                                                      mixed_meta_session):
        html = view(client, mixed_meta_session, q="good row")["html"]
        assert row_ids_in(html) == {"row2"}

    def test_search_no_match_shows_empty_state(self, client,
                                               mixed_meta_session):
        html = view(client, mixed_meta_session, q="zzz-nothing")["html"]
        assert row_ids_in(html) == set()
        assert "No rows matching" in html

    def test_search_finds_edited_values(self, client, mixed_meta_session):
        sid = mixed_meta_session
        client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "2-title-0",
            "new_value": "UniqueNeedle"})
        html = view(client, sid, q="uniqueneedle")["html"]
        assert row_ids_in(html) == {"row2"}

    def test_search_ands_with_changes_only(self, client, mixed_meta_session):
        sid = mixed_meta_session
        client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "2-title-0",
            "new_value": "UniqueNeedle"})
        html = view(client, sid, q="Dup Title", changes_only="true")["html"]
        assert row_ids_in(html) == set()


class TestIssueFilter:
    def test_single_issue_view(self, client, mixed_meta_session):
        body = view(client, mixed_meta_session, issue_id="meta-1")
        assert row_ids_in(body["html"]) == {"row1"}
        assert "filter-banner" in body["html"]
        assert "Filtered by issue" in body["html"]
        assert 'data-exit-filter="meta"' in body["html"]

    def test_unknown_issue_id_gives_empty_filtered_view(self, client,
                                                        mixed_meta_session):
        html = view(client, mixed_meta_session, issue_id="meta-99")["html"]
        assert row_ids_in(html) == set()
        assert "filter-banner" in html

    def test_facet_filter_by_issue_type(self, client, mixed_meta_session):
        # row1's error is a malformed br-id
        html = view(client, mixed_meta_session,
                    issue_types="br_id_format")["html"]
        assert row_ids_in(html) == {"row1"}
        assert "Filtered by issues" in html

    def test_facet_dropdown_lists_types_with_counts(self, client,
                                                    mixed_meta_session):
        html = view(client, mixed_meta_session)["html"]
        assert "Issue filters" in html
        assert 'data-label="br_id_format"' in html
        assert "1 row" in html
        assert 'data-facet-apply="meta"' in html
        assert 'data-facet-clear="meta"' in html

    def test_facet_with_unknown_type_is_empty(self, client,
                                              mixed_meta_session):
        html = view(client, mixed_meta_session,
                    issue_types="no_such_type")["html"]
        assert row_ids_in(html) == set()


class TestPagination:
    def test_first_page_has_page_size_rows(self, client, temp_env):
        sid = upload(client, temp_env, n_rows=30)
        body = view(client, sid, show_all="true")
        assert len(row_ids_in(body["html"])) == 25
        assert body["page"] == 1
        assert "Page 1 of 2" in body["html"]

    def test_second_page_has_the_remainder(self, client, temp_env):
        sid = upload(client, temp_env, n_rows=30)
        body = view(client, sid, show_all="true", page=2)
        assert len(row_ids_in(body["html"])) == 5
        assert body["page"] == 2

    def test_page_beyond_the_end_is_clamped(self, client, temp_env):
        sid = upload(client, temp_env, n_rows=30)
        body = view(client, sid, show_all="true", page=99)
        assert body["page"] == 2

    def test_focus_row_selects_its_page(self, client, temp_env):
        sid = upload(client, temp_env, n_rows=30)
        body = view(client, sid, show_all="true", focus_row_id="row29")
        assert body["page"] == 2

    def test_pager_buttons_are_rendered(self, client, temp_env):
        sid = upload(client, temp_env, n_rows=30)
        html = view(client, sid, show_all="true")["html"]
        assert 'data-pager="meta"' in html
        assert 'data-page="2"' in html


def upload(client, temp_env, n_rows):
    from test.support import upload_csvs
    return upload_csvs(client, meta=make_meta_csv(n_rows),
                       meta_name="big.csv")


class TestStatsHeader:
    def test_header_counts_errors_from_the_report(self, client,
                                                  invalid_meta_session):
        html = view(client, invalid_meta_session, show_all="true")["html"]
        assert "Issues: 3 (errors: 3; warnings: 0)" in html
        assert "Invalid rows: 3 | Total rows: 3" in html
        assert '<h4>Metadata</h4>' in html
        assert 'data-table-type="meta"' in html

    def test_header_counts_live_rows_after_edits(self, client,
                                                 invalid_meta_session):
        sid = invalid_meta_session
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row0"})
        html = view(client, sid, show_all="true")["html"]
        # 2 live rows, 2 of them still invalid
        assert "Invalid rows: 2 | Total rows: 2" in html

    def test_paired_session_renders_both_headers(self, client,
                                                 paired_session):
        html = view(client, paired_session, show_all="true",
                    cits_show_all="true")["html"]
        assert '<h4>Metadata</h4>' in html
        assert '<h4>Citations</h4>' in html
        assert html.count('id="table-data"') == 2
        assert 'data-table-type="meta"' in html
        assert 'data-table-type="cits"' in html

    def test_per_table_controls_are_scoped(self, client, paired_session):
        html = view(client, paired_session, show_all="true",
                    cits_changes_only="true")["html"]
        # independent per-table toggles
        assert 'data-table-type="meta"' in html
        assert 'data-table-type="cits"' in html
        assert html.count("table-controls") == 2
        assert html.count("table-search") == 2


class TestDegradedView:
    def test_missing_baseline_serves_zero_stats_cards(self, client, temp_env,
                                                      invalid_meta_session):
        sid = invalid_meta_session
        sdir = temp_env / sid
        for name in ("baseline_meta.html", "artifacts_meta.json",
                     "meta_table.html"):
            (sdir / name).unlink()
        # evict the in-RAM parsed state so the missing baseline is seen
        from services import view_builder
        view_builder._STATE_CACHE.clear()

        resp = client.get(f"/api/edit/table/{sid}")
        assert resp.status_code == 200
        html = resp.json()["html"]
        assert "table-stats" in html
        assert 'id="table-data"' not in html   # nothing editable
        assert '<h4>Metadata</h4>' in html
