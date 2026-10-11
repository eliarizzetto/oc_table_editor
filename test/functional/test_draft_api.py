"""Functional tests for the draft management routes (routes/draft.py)."""
from test.support import META_VALID_CSV, upload_csvs


class TestListDrafts:
    def test_list_includes_uploaded_session(self, client,
                                            invalid_meta_session):
        resp = client.get("/api/draft/list")
        assert resp.status_code == 200
        drafts = resp.json()["drafts"]
        ids = [d["session_id"] for d in drafts]
        assert invalid_meta_session in ids

        entry = next(d for d in drafts if d["session_id"] == invalid_meta_session)
        assert entry["has_metadata"] is True
        assert entry["has_citations"] is False

    def test_display_name_falls_back_to_csv_filename(self, client,
                                                     invalid_meta_session):
        drafts = client.get("/api/draft/list").json()["drafts"]
        entry = next(d for d in drafts
                     if d["session_id"] == invalid_meta_session)
        assert entry["draft_name"] is None
        assert entry["display_name"] == "invalid_metadata"

    def test_list_orders_most_recently_updated_first(self, client,
                                                     temp_env):
        first = upload_csvs(client, meta=META_VALID_CSV, meta_name="a.csv")
        second = upload_csvs(client, meta=META_VALID_CSV, meta_name="b.csv")
        # touching the FIRST session makes it the most recently updated
        client.post("/api/draft/rename", json={"session_id": first,
                                               "draft_name": "Touched"})

        drafts = client.get("/api/draft/list").json()["drafts"]
        assert drafts[0]["session_id"] == first
        assert [d["session_id"] for d in drafts].index(second) == 1

    def test_empty_list_when_no_sessions(self, client, temp_env):
        resp = client.get("/api/draft/list")
        assert resp.status_code == 200
        assert resp.json()["drafts"] == []


class TestRenameDraft:
    def test_rename(self, client, invalid_meta_session):
        resp = client.post("/api/draft/rename", json={
            "session_id": invalid_meta_session, "draft_name": "  Renamed  "})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["draft_name"] == "Renamed"   # stripped

        drafts = client.get("/api/draft/list").json()["drafts"]
        entry = next(d for d in drafts
                     if d["session_id"] == invalid_meta_session)
        assert entry["draft_name"] == "Renamed"
        assert entry["display_name"] == "Renamed"

    def test_rename_to_whitespace_only_is_400(self, client,
                                              invalid_meta_session):
        resp = client.post("/api/draft/rename", json={
            "session_id": invalid_meta_session, "draft_name": "   "})
        assert resp.status_code == 400

    def test_rename_to_overlong_name_is_400(self, client,
                                            invalid_meta_session):
        resp = client.post("/api/draft/rename", json={
            "session_id": invalid_meta_session, "draft_name": "x" * 121})
        assert resp.status_code == 400

    def test_rename_unknown_session_is_404(self, client, temp_env):
        resp = client.post("/api/draft/rename", json={
            "session_id": "nope", "draft_name": "X"})
        assert resp.status_code == 404


class TestDeleteDraft:
    def test_delete_removes_the_session_directory(self, client, temp_env,
                                                  invalid_meta_session):
        assert (temp_env / invalid_meta_session).exists()
        resp = client.delete(f"/api/draft/{invalid_meta_session}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not (temp_env / invalid_meta_session).exists()

        ids = [d["session_id"] for d in
               client.get("/api/draft/list").json()["drafts"]]
        assert invalid_meta_session not in ids

    def test_delete_unknown_draft_is_404(self, client, temp_env):
        assert client.delete("/api/draft/nope").status_code == 404

    def test_deleted_session_is_no_longer_editable(self, client, temp_env,
                                                   invalid_meta_session):
        sid = invalid_meta_session
        client.delete(f"/api/draft/{sid}")
        resp = client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "0-title-0", "new_value": "x"})
        assert resp.status_code == 404
        assert client.get(f"/editor/{sid}").status_code == 404
