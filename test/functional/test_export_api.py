"""Functional tests for the export route (routes/export.py)."""
import csv
from io import StringIO


def export(client, sid, **body_extra):
    body = {"session_id": sid, **body_extra}
    return client.post("/api/export/", json=body)


def parse_csv(text):
    return list(csv.reader(StringIO(text)))


class TestExport:
    def test_export_returns_the_original_data_unchanged(self, client,
                                                        invalid_meta_session):
        resp = export(client, invalid_meta_session)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert resp.headers["content-disposition"] == (
            'attachment; filename=metadata_edited.csv')

        rows = parse_csv(resp.text)
        assert rows[0][0] == "id"
        assert len(rows) == 4                       # header + 3 rows
        assert rows[1][0] == "arxiv:0711.3834 arxiv:0711.3834"

    def test_export_reflects_edits_additions_and_deletions(
            self, client, invalid_meta_session):
        sid = invalid_meta_session
        client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "0-title-0",
            "new_value": "Exported Title"})
        client.post("/api/edit/item/add", json={          # multi-value add
            "session_id": sid, "row_id": "row1", "field_name": "id",
            "new_value": "doi:10.9999/added"})
        client.post("/api/edit/row/add", json={"session_id": sid})
        client.post("/api/edit/item/add", json={          # fill the new row
            "session_id": sid, "row_id": "row3", "field_name": "id",
            "new_value": "doi:10.9999/new-row"})
        client.post("/api/edit/row/delete", json={        # delete a row
            "session_id": sid, "row_id": "row2"})

        rows = parse_csv(export(client, sid).text)
        assert len(rows) == 4            # header + 3 live rows
        assert rows[1][1] == "Exported Title"
        assert rows[2][0] == "arxiv0905.1997 doi:10.9999/added"
        assert rows[3][0] == "doi:10.9999/new-row"
        assert all("2010/07" not in r[3] for r in rows[1:])

    def test_export_filters_blank_items_no_stray_separators(
            self, client, invalid_meta_session):
        sid = invalid_meta_session
        # edit-to-empty on a multi-value item removes it entirely
        client.post("/api/edit/item", json={
            "session_id": sid, "item_id": "0-id-1", "new_value": ""})
        rows = parse_csv(export(client, sid).text)
        assert rows[1][0] == "arxiv:0711.3834"       # single value, no gap

    def test_export_citations_table_of_paired_session(self, client,
                                                      paired_session):
        resp = export(client, paired_session, table_type="cits")
        assert resp.status_code == 200
        assert 'filename=citations_edited.csv' in \
            resp.headers["content-disposition"]
        rows = parse_csv(resp.text)
        assert rows[0] == ["citing_id", "citing_publication_date",
                           "cited_id", "cited_publication_date"]
        assert len(rows) == 3

    def test_export_with_revalidate_and_no_edits_skips(self, client,
                                                       invalid_meta_session):
        resp = export(client, invalid_meta_session, revalidate=True)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"warning": "No edits made since last validation. "
                                   "Re-validation skipped.",
                        "exported": False}

    def test_export_unknown_session_is_404(self, client, temp_env):
        assert export(client, "nope").status_code == 404

    def test_export_invalid_table_type_is_422(self, client,
                                              invalid_meta_session):
        assert export(client, invalid_meta_session,
                      table_type="bogus").status_code == 422

    def test_export_table_type_absent_from_session_is_404(self, client,
                                                          invalid_meta_session):
        assert export(client, invalid_meta_session,
                      table_type="cits").status_code == 404
