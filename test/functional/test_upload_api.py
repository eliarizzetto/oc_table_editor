"""Functional tests for the upload route (routes/upload.py)."""
import json

from test.support import (CITS_INVALID_CSV, CITS_VALID_CSV, META_HEADERS,
                          META_INVALID_CSV, META_VALID_CSV, upload_csvs)


def post_upload(client, files=None, **data):
    return client.post("/api/upload/", files=files or {}, data=data)


class TestSuccessfulUploads:
    def test_invalid_metadata_upload(self, client, temp_env):
        resp = post_upload(client, {
            "metadata_file": ("invalid_metadata.csv",
                              META_INVALID_CSV.encode("utf-8"), "text/csv")})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"session_id", "html_url"}
        assert body["html_url"] == f"/editor/{body['session_id']}"

        sdir = temp_env / body["session_id"]
        names = {p.name for p in sdir.iterdir()}
        # uploaded CSV (original filename), report, commit artifact,
        # baseline, artifacts, journal, session record
        assert {"invalid_metadata.csv", "meta_table.html",
                "baseline_meta.html", "artifacts_meta.json",
                "journal_meta.jsonl", "journal_meta_state.json",
                "session.json"} <= names
        assert any(n.startswith("out_validate_meta") and n.endswith(".jsonl")
                   for n in names)

    def test_session_json_records_flags_and_paths(self, client, temp_env):
        sid = upload_csvs(client, meta=META_INVALID_CSV)
        session = json.loads((temp_env / sid / "session.json").read_text(
            encoding="utf-8"))
        assert session["has_metadata"] is True
        assert session["has_citations"] is False
        assert session["verify_id_existence"] is False
        assert session["has_edits_since_validation"] is False
        assert session["meta_csv_path"].endswith("meta.csv")
        assert session["meta_report_path"] is not None

    def test_valid_metadata_upload_still_gets_a_table(self, client, temp_env,
                                                      valid_meta_session):
        """The empty-report workaround: valid tables keep an editable table."""
        sdir = temp_env / valid_meta_session
        assert (sdir / "baseline_meta.html").read_text(
            encoding="utf-8").count('id="table-data"') == 1
        assert (sdir / "artifacts_meta.json").exists()

    def test_citations_only_upload(self, client, temp_env):
        sid = upload_csvs(client, cits=CITS_INVALID_CSV, cits_name="cits.csv")
        names = {p.name for p in (temp_env / sid).iterdir()}
        assert {"cits_table.html", "baseline_cits.html",
                "artifacts_cits.json", "journal_cits.jsonl"} <= names
        assert "meta_table.html" not in names

        resp = client.get(f"/api/edit/table/{sid}")
        assert resp.status_code == 200
        # solo citations session: the citations table IS the primary
        assert resp.json()["table_type"] == "cits"

    def test_paired_upload_creates_both_journals(self, client, temp_env):
        sid = upload_csvs(client, meta=META_VALID_CSV, cits=CITS_INVALID_CSV)
        names = {p.name for p in (temp_env / sid).iterdir()}
        assert {"meta_table.html", "cits_table.html",
                "baseline_meta.html", "baseline_cits.html",
                "artifacts_meta.json", "artifacts_cits.json",
                "journal_meta.jsonl", "journal_meta_state.json",
                "journal_cits.jsonl", "journal_cits_state.json"} <= names

    def test_paired_upload_with_same_filename_prefixes_citations(
            self, client, temp_env):
        sid = upload_csvs(client, meta=META_VALID_CSV, cits=CITS_VALID_CSV,
                          meta_name="table.csv", cits_name="table.csv")
        names = {p.name for p in (temp_env / sid).iterdir()}
        assert "table.csv" in names        # the metadata copy
        assert "cits_table.csv" in names   # prefixed citations copy

    def test_draft_name_is_stored(self, client, temp_env):
        sid = upload_csvs(client, meta=META_VALID_CSV, draft_name="My Draft")
        session = json.loads((temp_env / sid / "session.json").read_text(
            encoding="utf-8"))
        assert session["draft_name"] == "My Draft"

    def test_verify_id_existence_flag_is_stored(self, client, temp_env):
        sid = upload_csvs(client, meta=META_VALID_CSV,
                          verify_id_existence="true")
        session = json.loads((temp_env / sid / "session.json").read_text(
            encoding="utf-8"))
        assert session["verify_id_existence"] is True


class TestUploadValidationErrors:
    def test_no_files_is_400(self, client, temp_env):
        resp = post_upload(client)
        assert resp.status_code == 400
        assert "At least one CSV file" in resp.json()["detail"]

    def test_wrong_table_type_is_400_and_cleans_up(self, client, temp_env):
        resp = post_upload(client, {
            "metadata_file": ("cits.csv", CITS_VALID_CSV.encode("utf-8"),
                              "text/csv")})
        assert resp.status_code == 400
        # the failed session must not linger on disk
        assert [d.name for d in temp_env.iterdir() if d.is_dir()] == []

    def test_metadata_as_citations_is_400(self, client, temp_env):
        resp = post_upload(client, {
            "citations_file": ("meta.csv", META_VALID_CSV.encode("utf-8"),
                               "text/csv")})
        assert resp.status_code == 400

    def test_header_with_angle_brackets_is_400(self, client, temp_env):
        bad = "id,title>,author\nx,y,z\n"
        resp = post_upload(client, {
            "metadata_file": ("bad.csv", bad.encode("utf-8"), "text/csv")})
        assert resp.status_code == 400
        assert "header contains characters" in resp.json()["detail"]

    def test_header_only_csv_is_400(self, client, temp_env):
        resp = post_upload(client, {
            "metadata_file": ("ho.csv", (META_HEADERS + "\n").encode("utf-8"),
                              "text/csv")})
        assert resp.status_code == 400
        assert "CSV" in resp.json()["detail"]

    def test_overlong_draft_name_is_400(self, client, temp_env):
        resp = post_upload(client, {
            "metadata_file": ("m.csv", META_VALID_CSV.encode("utf-8"),
                              "text/csv")}, draft_name="x" * 121)
        assert resp.status_code == 400
        assert "at most 120 characters" in resp.json()["detail"]


class TestArtifactsContract:
    def test_artifacts_describe_the_table(self, client, temp_env,
                                          invalid_meta_session):
        art = json.loads(
            (temp_env / invalid_meta_session / "artifacts_meta.json")
            .read_text(encoding="utf-8"))
        assert art["has_table"] is True
        assert art["headers"][0] == "id"
        assert art["row_ids"] == ["row0", "row1", "row2"]
        # the duplicated id cell holds TWO items
        assert len(art["rows"]["row0"]["id"]) == 2
        # one issue per row (3 report lines, 3 rows involved)
        assert sorted(art["issue_index"].values(),
                      key=lambda rows: rows[0]) == [["row0"], ["row1"],
                                                    ["row2"]]
