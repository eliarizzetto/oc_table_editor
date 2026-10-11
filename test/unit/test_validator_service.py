"""Unit/integration tests for the oc_validator wrapper (services/validator_service.py).

These run the real (offline — ``verify_id_existence=False``) validator on
tiny CSVs; they use the shared constants from conftest.
"""
import json

import pytest
from oc_validator.main import TableNotMatchingInstance

from services.validator_service import (ValidatorService, load_jsonl_report)
from test.support import (CITS_INVALID_CSV, CITS_VALID_CSV, META_INVALID_CSV,
                          META_VALID_CSV)


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def write_csv(directory, name, content):
    path = directory / name
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


class TestLoadJsonlReport:
    def test_reads_each_line_as_a_dict(self, tmp_path):
        report = tmp_path / "r.jsonl"
        report.write_text(
            json.dumps({"error_type": "error", "error_label": "duplicate_id"})
            + "\n"
            + json.dumps({"error_type": "warning", "error_label": "date"})
            + "\n\n",  # blank lines are skipped
            encoding="utf-8")
        entries = load_jsonl_report(str(report))
        assert len(entries) == 2
        assert entries[0]["error_label"] == "duplicate_id"

    def test_empty_file_gives_empty_list(self, tmp_path):
        report = tmp_path / "empty.jsonl"
        report.write_text("", encoding="utf-8")
        assert load_jsonl_report(str(report)) == []


class TestValidateSingle:
    def test_invalid_metadata_reports_errors(self, workdir):
        csv_path = write_csv(workdir, "meta.csv", META_INVALID_CSV)
        is_valid, report_path = ValidatorService.validate_metadata(
            csv_path, str(workdir))
        assert is_valid is False
        entries = load_jsonl_report(report_path)
        assert len(entries) == 3
        assert all(e["error_type"] == "error" for e in entries)
        labels = {e["error_label"] for e in entries}
        assert labels == {"duplicate_id", "br_id_format", "date_format"}

    def test_valid_metadata_gives_empty_report(self, workdir):
        csv_path = write_csv(workdir, "meta.csv", META_VALID_CSV)
        is_valid, report_path = ValidatorService.validate_metadata(
            csv_path, str(workdir))
        assert is_valid is True
        assert load_jsonl_report(report_path) == []

    def test_invalid_citations_reports_errors(self, workdir):
        csv_path = write_csv(workdir, "cits.csv", CITS_INVALID_CSV)
        is_valid, report_path = ValidatorService.validate_citations(
            csv_path, str(workdir))
        assert is_valid is False
        entries = load_jsonl_report(report_path)
        assert len(entries) >= 1
        assert entries[0]["error_label"] == "br_id_syntax"

    def test_valid_citations_passes(self, workdir):
        csv_path = write_csv(workdir, "cits.csv", CITS_VALID_CSV)
        is_valid, report_path = ValidatorService.validate_citations(
            csv_path, str(workdir))
        assert is_valid is True
        assert load_jsonl_report(report_path) == []

    def test_citations_table_as_metadata_is_rejected(self, workdir):
        csv_path = write_csv(workdir, "wrong.csv", CITS_VALID_CSV)
        with pytest.raises(TableNotMatchingInstance):
            ValidatorService.validate_metadata(csv_path, str(workdir))

    def test_metadata_table_as_citations_is_rejected(self, workdir):
        csv_path = write_csv(workdir, "wrong.csv", META_VALID_CSV)
        with pytest.raises(TableNotMatchingInstance):
            ValidatorService.validate_citations(csv_path, str(workdir))


class TestValidatePair:
    def test_valid_pair_passes_closure(self, workdir):
        meta_csv = write_csv(workdir, "m.csv", META_VALID_CSV)
        cits_csv = write_csv(workdir, "c.csv", CITS_VALID_CSV)
        meta_ok, cits_ok, meta_rep, cits_rep = ValidatorService.validate_pair(
            meta_csv, cits_csv, str(workdir), str(workdir))
        assert meta_ok is True and cits_ok is True
        assert load_jsonl_report(meta_rep) == []
        assert load_jsonl_report(cits_rep) == []

    def test_pair_with_bad_citations_reports_them(self, workdir):
        meta_csv = write_csv(workdir, "m.csv", META_VALID_CSV)
        cits_csv = write_csv(workdir, "c.csv", CITS_INVALID_CSV)
        meta_ok, cits_ok, _meta_rep, cits_rep = ValidatorService.validate_pair(
            meta_csv, cits_csv, str(workdir), str(workdir))
        assert cits_ok is False
        assert len(load_jsonl_report(cits_rep)) >= 1


class TestTableHtmlGeneration:
    def test_valid_table_gets_an_editable_table(self, workdir):
        """The empty-report workaround: valid tables keep a real table."""
        csv_path = write_csv(workdir, "meta.csv", META_VALID_CSV)
        out = workdir / "table.html"
        ValidatorService.make_valid_table_html(str(out), csv_path, "Metadata")

        html = out.read_text(encoding="utf-8")
        assert 'id="table-data"' in html
        assert 'id="row0"' in html
        assert 'class="item-container"' in html
        assert 'class="issue-icon' not in html  # zero issues

    def test_valid_table_markup_matches_make_gui_contract(self, workdir):
        csv_path = write_csv(workdir, "meta.csv", META_VALID_CSV)
        out = workdir / "table.html"
        ValidatorService.make_valid_table_html(str(out), csv_path, "Metadata")

        html = out.read_text(encoding="utf-8")
        assert '<td class="row-number">0</td>' in html
        assert '<td class="field-value id">' in html
        assert 'id="0-id-0"' in html

    def test_no_errors_fallback_writes_a_card_without_table(self, workdir):
        csv_path = write_csv(workdir, "meta.csv", META_VALID_CSV)
        out = workdir / "card.html"
        ValidatorService._make_no_errors_html(str(out), csv_path, "Citations")

        html = out.read_text(encoding="utf-8")
        assert "<h4>Citations</h4>" in html
        assert "No issues found" in html
        assert "<table" not in html

    def test_generate_table_html_delegates_on_valid(self, workdir):
        csv_path = write_csv(workdir, "meta.csv", META_VALID_CSV)
        direct = workdir / "direct.html"
        wrapped = workdir / "wrapped.html"
        ValidatorService.make_valid_table_html(str(direct), csv_path,
                                               "Metadata")
        ValidatorService.generate_table_html(csv_path, str(workdir / "r.jsonl"),
                                             str(wrapped), True, "Metadata")
        assert direct.read_text(encoding="utf-8") == \
            wrapped.read_text(encoding="utf-8")

    def test_generate_table_html_invalid_uses_make_gui(self, workdir):
        """The invalid path goes through make_gui: issue icons are rendered."""
        csv_path = write_csv(workdir, "meta.csv", META_INVALID_CSV)
        _is_valid, report_path = ValidatorService.validate_metadata(
            csv_path, str(workdir))
        out = workdir / "table.html"
        ValidatorService.generate_table_html(csv_path, report_path,
                                             str(out), False, "Metadata")

        html = out.read_text(encoding="utf-8")
        assert 'id="table-data"' in html
        assert 'class="issue-icon' in html
        # the duplicate-id error marks the first row's id items
        assert 'duplicate_id' in html
