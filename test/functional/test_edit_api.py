"""Functional tests for the mutation routes (routes/edit.py).

The invalid metadata fixture has a known shape:
- row0: id cell holds TWO items (0-id-0, 0-id-1 — the duplicated arxiv id)
- row1: id item 1-id-0 is a malformed br-id
- row2: pub_date 2-pub_date-0 is '2010/07'
"""


def edit_item(client, sid, item_id, new_value, table_type=None):
    body = {"session_id": sid, "item_id": item_id, "new_value": new_value}
    if table_type:
        body["table_type"] = table_type
    return client.post("/api/edit/item", json=body)


def get_view_html(client, sid, **params):
    resp = client.get(f"/api/edit/table/{sid}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["html"]


class TestEditItem:
    def test_set_item_response_and_view(self, client, invalid_meta_session):
        sid = invalid_meta_session
        resp = edit_item(client, sid, "0-title-0", "Edited Title")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "row_id": "row0",
                               "table_type": "meta"}

        html = get_view_html(client, sid)
        assert "Edited Title" in html
        assert 'class="item-data edited"' in html

    def test_set_item_persists_in_journal(self, client, temp_env,
                                          invalid_meta_session):
        edit_item(client, invalid_meta_session, "0-title-0", "Edited Title")
        journal = (temp_env / invalid_meta_session
                   / "journal_meta.jsonl").read_text(encoding="utf-8")
        assert '"op": "set_item"' in journal
        assert '"value": "Edited Title"' in journal

    def test_edit_to_empty_on_multi_value_is_a_removal(self, client,
                                                       invalid_meta_session):
        sid = invalid_meta_session
        resp = edit_item(client, sid, "0-id-1", "   ")
        assert resp.status_code == 200

        html = get_view_html(client, sid)
        # a ghost container shows the removed value
        assert 'id="ghost-0-id-1"' in html
        assert "deleted-ghost" in html

    def test_edit_to_empty_on_single_value_is_allowed(self, client,
                                                      invalid_meta_session):
        sid = invalid_meta_session
        resp = edit_item(client, sid, "2-title-0", "")
        assert resp.status_code == 200
        # the item stays (emptied) — export drops its value only
        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        assert "Trudinger-Moser" not in csv_text
        assert len(csv_text.splitlines()) == 4  # header + all 3 rows

    def test_unknown_item_is_404(self, client, invalid_meta_session):
        resp = edit_item(client, invalid_meta_session, "9-title-0", "x")
        assert resp.status_code == 404

    def test_unknown_session_is_404(self, client, temp_env):
        resp = client.post("/api/edit/item", json={
            "session_id": "nope", "item_id": "0-title-0", "new_value": "x"})
        assert resp.status_code == 404


class TestAddItem:
    def test_add_to_multi_value_cell(self, client, invalid_meta_session):
        sid = invalid_meta_session
        resp = client.post("/api/edit/item/add", json={
            "session_id": sid, "row_id": "row1", "field_name": "id",
            "new_value": "doi:10.9999/added"})
        assert resp.status_code == 200

        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        row1 = csv_text.splitlines()[2]
        assert "arxiv0905.1997 doi:10.9999/added" in row1  # space-joined

    def test_add_to_empty_cell_uses_init_cell(self, client,
                                              invalid_meta_session):
        sid = invalid_meta_session
        resp = client.post("/api/edit/item/add", json={
            "session_id": sid, "row_id": "row0", "field_name": "publisher",
            "new_value": "ACME"})
        assert resp.status_code == 200

        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        assert ",ACME," in csv_text

    def test_add_to_valued_single_field_is_400(self, client,
                                               invalid_meta_session):
        resp = client.post("/api/edit/item/add", json={
            "session_id": invalid_meta_session, "row_id": "row0",
            "field_name": "title", "new_value": "Second Title"})
        assert resp.status_code == 400
        assert "single-value field" in resp.json()["detail"]

    def test_add_to_unknown_row_is_404(self, client, invalid_meta_session):
        resp = client.post("/api/edit/item/add", json={
            "session_id": invalid_meta_session, "row_id": "row9",
            "field_name": "id", "new_value": "doi:10.1/x"})
        assert resp.status_code == 404


class TestDeleteItem:
    def test_delete_item(self, client, invalid_meta_session):
        sid = invalid_meta_session
        resp = client.request("DELETE", "/api/edit/item", json={
            "session_id": sid, "item_id": "0-id-1"})
        assert resp.status_code == 200

        html = get_view_html(client, sid)
        assert 'id="ghost-0-id-1"' in html
        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        row0 = csv_text.splitlines()[1]
        assert row0.startswith("arxiv:0711.3834,")

    def test_delete_missing_item_is_a_success_noop(self, client,
                                                   invalid_meta_session):
        resp = client.request("DELETE", "/api/edit/item", json={
            "session_id": invalid_meta_session, "item_id": "0-id-1"})
        assert resp.status_code == 200
        resp = client.request("DELETE", "/api/edit/item", json={
            "session_id": invalid_meta_session, "item_id": "0-id-1"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestClearCell:
    def test_clear_cell_ghosts_all_items(self, client, invalid_meta_session):
        sid = invalid_meta_session
        resp = client.post("/api/edit/cell/clear", json={
            "session_id": sid, "row_id": "row0", "field_name": "id"})
        assert resp.status_code == 200

        html = get_view_html(client, sid)
        assert 'id="ghost-0-id-0"' in html
        assert 'id="ghost-0-id-1"' in html
        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        row0 = csv_text.splitlines()[1]
        assert row0.startswith(",")

    def test_clear_unknown_field_is_404(self, client, invalid_meta_session):
        resp = client.post("/api/edit/cell/clear", json={
            "session_id": invalid_meta_session, "row_id": "row0",
            "field_name": "no_such_field"})
        assert resp.status_code == 404


class TestRowOperations:
    def test_add_row_appears_last_and_exported(self, client,
                                               invalid_meta_session):
        sid = invalid_meta_session
        resp = client.post("/api/edit/row/add", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["row_id"] == "row3"  # baseline rows are 0-2

        html = get_view_html(client, sid, show_all="true")
        assert 'id="row3"' in html
        assert 'class="added"' in html

        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        assert len(csv_text.splitlines()) == 5  # header + 4 rows

    def test_delete_row_renders_ghost_and_drops_from_export(
            self, client, invalid_meta_session):
        sid = invalid_meta_session
        resp = client.post("/api/edit/row/delete", json={
            "session_id": sid, "row_id": "row2"})
        assert resp.status_code == 200

        html = get_view_html(client, sid)
        assert 'data-ghost-row-id="row2"' in html
        csv_text = client.post("/api/export/", json={"session_id": sid}).text
        assert "2010/07" not in csv_text  # the deleted row's data is gone

    def test_delete_missing_row_is_a_success_noop(self, client,
                                                  invalid_meta_session):
        resp = client.post("/api/edit/row/delete", json={
            "session_id": invalid_meta_session, "row_id": "row9"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_added_row_ids_are_never_reused(self, client,
                                            invalid_meta_session):
        sid = invalid_meta_session
        for expected in ("row3", "row4"):
            resp = client.post("/api/edit/row/add", json={"session_id": sid})
            assert resp.json()["row_id"] == expected
        # delete both, then add again: ids keep growing
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row3"})
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row4"})
        resp = client.post("/api/edit/row/add", json={"session_id": sid})
        assert resp.json()["row_id"] == "row5"

    def test_added_then_deleted_row_leaves_no_ghost(self, client,
                                                    invalid_meta_session):
        sid = invalid_meta_session
        client.post("/api/edit/row/add", json={"session_id": sid})  # row3
        client.post("/api/edit/row/delete", json={"session_id": sid,
                                                  "row_id": "row3"})
        html = get_view_html(client, sid, show_all="true")
        assert 'id="row3"' not in html
        assert 'data-ghost-row-id="row3"' not in html


class TestTableTypeRouting:
    def test_edit_citations_table_of_paired_session(self, client,
                                                     paired_session):
        sid = paired_session
        resp = edit_item(client, sid, "0-cited_id-0", "arxiv:0905.1997",
                         table_type="cits")
        assert resp.status_code == 200
        assert resp.json()["table_type"] == "cits"

        resp = client.get(f"/api/edit/table/{sid}",
                          params={"cits_show_all": "true"})
        assert "arxiv:0905.1997" in resp.json()["html"]

    def test_invalid_table_type_is_422(self, client, invalid_meta_session):
        resp = edit_item(client, invalid_meta_session, "0-title-0", "x",
                         table_type="bibliography")
        assert resp.status_code == 422

    def test_table_type_absent_from_session_is_404(self, client,
                                                   invalid_meta_session):
        resp = edit_item(client, invalid_meta_session, "0-title-0", "x",
                         table_type="cits")
        assert resp.status_code == 404


class TestSessionInfo:
    def test_session_endpoint_reflects_edits(self, client,
                                              invalid_meta_session):
        sid = invalid_meta_session
        resp = client.get(f"/api/edit/session/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["edited_items_count"] == 0
        assert body["unsaved_changes"] is False
        assert body["can_undo"] is False

        edit_item(client, sid, "0-title-0", "Edited")
        body = client.get(f"/api/edit/session/{sid}").json()
        assert body["edited_items_count"] == 1
        assert body["unsaved_changes"] is True
        assert body["can_undo"] is True
        assert body["has_edits_since_validation"] is True

    def test_unknown_session_is_404(self, client, temp_env):
        assert client.get("/api/edit/session/nope").status_code == 404
