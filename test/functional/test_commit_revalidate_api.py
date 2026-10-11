"""Functional tests for commit (Save) and revalidate."""


def edit_item(client, sid, item_id, new_value, table_type=None):
    body = {"session_id": sid, "item_id": item_id, "new_value": new_value}
    if table_type:
        body["table_type"] = table_type
    return client.post("/api/edit/item", json=body)


class TestCommit:
    def test_commit_materializes_the_view(self, client, temp_env,
                                          invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Committed Title")
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row2"})

        resp = client.post("/api/edit/commit", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "saved": True}

        table = (temp_env / sid / "meta_table.html").read_text(
            encoding="utf-8")
        assert "Committed Title" in table
        assert "On the Analytic Wavelet Transform" not in table  # replaced
        assert "2010/07" not in table          # deleted row materialized out

    def test_commit_clears_the_unsaved_flag(self, client,
                                             invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Committed Title")
        assert client.get(f"/api/edit/session/{sid}").json()[
            "unsaved_changes"] is True

        client.post("/api/edit/commit", json={"session_id": sid})
        assert client.get(f"/api/edit/session/{sid}").json()[
            "unsaved_changes"] is False

    def test_undo_history_survives_the_commit(self, client,
                                              invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Committed Title")
        client.post("/api/edit/commit", json={"session_id": sid})

        body = client.get(f"/api/edit/session/{sid}").json()
        assert body["can_undo"] is True
        client.post("/api/edit/undo", json={"session_id": sid})
        assert client.get(f"/api/edit/session/{sid}").json()[
            "unsaved_changes"] is True

    def test_commit_unknown_session_is_404(self, client, temp_env):
        resp = client.post("/api/edit/commit", json={"session_id": "nope"})
        assert resp.status_code == 404


class TestRevalidate:
    def test_revalidate_recounts_errors(self, client, invalid_meta_session):
        sid = invalid_meta_session
        # fix row1's malformed id -> 3 errors become 2
        edit_item(client, sid, "1-id-0", "doi:10.1234/fixed")

        resp = client.post("/api/edit/revalidate", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "error_count": 2}

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true"}).json()["html"]
        assert "Issues: 2 (errors: 2; warnings: 0)" in html

    def test_revalidate_resets_the_journals(self, client,
                                            invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Edited")
        resp = client.post("/api/edit/revalidate", json={"session_id": sid})
        assert resp.status_code == 200

        body = client.get(f"/api/edit/session/{sid}").json()
        assert body["can_undo"] is False
        assert body["can_redo"] is False
        assert body["unsaved_changes"] is False
        assert body["has_edits_since_validation"] is False

    def test_revalidate_bakes_edits_into_the_new_baseline(self, client,
                                                          temp_env,
                                                          invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Survives Revalidation")
        client.post("/api/edit/revalidate", json={"session_id": sid})

        baseline = (temp_env / sid / "baseline_meta.html").read_text(
            encoding="utf-8")
        assert "Survives Revalidation" in baseline

    def test_revalidate_drops_deleted_rows(self, client,
                                            invalid_meta_session):
        sid = invalid_meta_session
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row1"})
        resp = client.post("/api/edit/revalidate", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["error_count"] == 2  # row0 + row2 errors remain

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true"}).json()["html"]
        assert 'data-ghost-row-id="row1"' not in html
        assert "Total rows: 2" in html

    def test_revalidate_unknown_session_is_404(self, client, temp_env):
        resp = client.post("/api/edit/revalidate",
                           json={"session_id": "nope"})
        assert resp.status_code == 404

    def test_revalidate_paired_session_both_sides(self, client, temp_env):
        from test.support import CITS_VALID_CSV, META_VALID_CSV, upload_csvs
        sid = upload_csvs(client, meta=META_VALID_CSV, cits=CITS_VALID_CSV)
        # introduce a citations error, then revalidate: counted & visible
        edit_item(client, sid, "0-cited_id-0", "garbage-id", table_type="cits")
        resp = client.post("/api/edit/revalidate", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["error_count"] >= 1

        html = client.get(f"/api/edit/table/{sid}",
                          params={"cits_show_all": "true"}).json()["html"]
        assert html.count('id="table-data"') == 2   # both tables present

        # fix it, revalidate again: the pair is fully valid
        edit_item(client, sid, "0-cited_id-0", "arxiv:0905.1997", table_type="cits")
        resp = client.post("/api/edit/revalidate", json={"session_id": sid})
        assert resp.json()["error_count"] == 0

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true",
                                  "cits_show_all": "true"}).json()["html"]
        assert "No issues found" in html
        assert html.count('id="table-data"') == 2
