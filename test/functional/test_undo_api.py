"""Functional tests for the session-wide undo/redo stack."""


def undo(client, sid):
    return client.post("/api/edit/undo", json={"session_id": sid})


def redo(client, sid):
    return client.post("/api/edit/redo", json={"session_id": sid})


def edit_item(client, sid, item_id, new_value, table_type=None):
    body = {"session_id": sid, "item_id": item_id, "new_value": new_value}
    if table_type:
        body["table_type"] = table_type
    return client.post("/api/edit/item", json=body)


class TestSingleTableUndoRedo:
    def test_undo_reverts_the_last_edit(self, client, invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Edited")
        resp = undo(client, sid)
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "table_type": "meta",
                               "row_id": "row0"}

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true"}).json()["html"]
        assert "Edited" not in html

    def test_undo_and_redo_round_trip(self, client, invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "Edited")

        undo(client, sid)
        resp = redo(client, sid)
        assert resp.json()["success"] is True

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true"}).json()["html"]
        assert "Edited" in html

    def test_undo_on_fresh_session_reports_nothing(self, client,
                                                   invalid_meta_session):
        resp = undo(client, invalid_meta_session)
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_redo_on_fresh_session_reports_nothing(self, client,
                                                   invalid_meta_session):
        resp = redo(client, invalid_meta_session)
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_new_edit_destroys_the_redo_tail(self, client,
                                             invalid_meta_session):
        sid = invalid_meta_session
        edit_item(client, sid, "0-title-0", "First")
        edit_item(client, sid, "0-title-0", "Second")
        undo(client, sid)          # drop "Second"

        edit_item(client, sid, "0-title-0", "Third")  # truncates the tail
        resp = redo(client, sid)
        assert resp.json()["success"] is False

        html = client.get(f"/api/edit/table/{sid}",
                          params={"show_all": "true"}).json()["html"]
        assert "Third" in html and "Second" not in html

    def test_undo_restores_deleted_row(self, client, invalid_meta_session):
        sid = invalid_meta_session
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row1"})
        html = client.get(f"/api/edit/table/{sid}").json()["html"]
        assert 'data-ghost-row-id="row1"' in html

        undo(client, sid)
        html = client.get(f"/api/edit/table/{sid}").json()["html"]
        assert 'data-ghost-row-id="row1"' not in html
        assert 'id="row1"' in html

    def test_undo_state_endpoint(self, client, invalid_meta_session):
        sid = invalid_meta_session
        body = client.get(f"/api/edit/undo_state/{sid}").json()
        assert body == {"tables": {"meta": {"can_undo": False,
                                            "can_redo": False}},
                        "can_undo": False, "can_redo": False}

        edit_item(client, sid, "0-title-0", "Edited")
        undo(client, sid)
        body = client.get(f"/api/edit/undo_state/{sid}").json()
        assert body["tables"]["meta"] == {"can_undo": False,
                                          "can_redo": True}
        assert body["can_redo"] is True

    def test_undo_state_unknown_session_is_404(self, client, temp_env):
        assert client.get("/api/edit/undo_state/nope").status_code == 404


class TestSessionWideStack:
    def test_undo_walks_both_tables_in_gseq_order(self, client,
                                                  paired_session):
        sid = paired_session
        # meta edit first, cits edit second
        edit_item(client, sid, "0-title-0", "Meta Edit", table_type="meta")
        edit_item(client, sid, "0-cited_id-0", "arxiv:0905.1997",
                  table_type="cits")

        assert undo(client, sid).json()["table_type"] == "cits"
        assert undo(client, sid).json()["table_type"] == "meta"

        assert redo(client, sid).json()["table_type"] == "meta"
        assert redo(client, sid).json()["table_type"] == "cits"

    def test_undo_state_covers_both_journals(self, client, paired_session):
        sid = paired_session
        edit_item(client, sid, "0-title-0", "Meta Edit", table_type="meta")
        body = client.get(f"/api/edit/undo_state/{sid}").json()
        assert body["tables"] == {
            "meta": {"can_undo": True, "can_redo": False},
            "cits": {"can_undo": False, "can_redo": False}}

    def test_new_edit_in_one_table_destroys_redo_in_the_other(
            self, client, paired_session):
        sid = paired_session
        edit_item(client, sid, "0-cited_id-0", "arxiv:0905.1997",
                  table_type="cits")
        undo(client, sid)  # cits edit undone

        edit_item(client, sid, "0-title-0", "Meta Edit", table_type="meta")
        body = client.get(f"/api/edit/undo_state/{sid}").json()
        assert body["tables"]["cits"]["can_redo"] is False  # tail destroyed
