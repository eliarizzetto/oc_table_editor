"""Unit tests for the CSV exporter (services/csv_exporter.py)."""
from services.csv_exporter import CSVExporter


class TestGenerateCsv:
    def test_empty_rows_give_empty_string(self):
        assert CSVExporter.generate_csv([]) == ""

    def test_multi_value_field_separators(self):
        rows = [{"id": ["doi:10.1/a", "doi:10.1/b"],
                 "author": ["Smith, J.", "Doe, A."],
                 "title": ["A Title"]}]
        csv_text = CSVExporter.generate_csv(rows)
        line = csv_text.splitlines()[1]
        assert "doi:10.1/a doi:10.1/b" in line      # ids join with a space
        assert "Smith, J.; Doe, A." in line          # agents join with '; '

    def test_single_value_field_takes_first_item_only(self):
        rows = [{"page": ["12", "34"]}]
        line = CSVExporter.generate_csv(rows).splitlines()[1]
        assert line == "12"

    def test_blank_items_are_filtered_from_multi_value_joins(self):
        """Emptied item-containers must not leave stray separators."""
        rows = [{"id": ["doi:10.1/a", "  ", "doi:10.1/b"]}]
        line = CSVExporter.generate_csv(rows).splitlines()[1]
        assert line == "doi:10.1/a doi:10.1/b"

    def test_missing_single_value_exports_empty(self):
        import csv
        from io import StringIO
        rows = [{"title": []}]
        # the csv module quotes a lone empty field as "" — parse it back
        parsed = list(csv.reader(StringIO(
            CSVExporter.generate_csv(rows).splitlines()[1])))
        assert parsed == [[""]]

    def test_header_comes_from_first_row(self):
        rows = [{"id": ["a"], "title": ["t"]}]
        assert CSVExporter.generate_csv(rows).splitlines()[0] == "id,title"


class TestRowsToCsv:
    def test_writes_original_comma_header(self, tmp_path):
        original = tmp_path / "meta.csv"
        original.write_text("id,title,author\ndoi:1,T,A\n", encoding="utf-8")
        rows = [{"id": ["doi:1"], "title": ["Edited"], "author": ["A"]}]

        csv_text = CSVExporter.rows_to_csv(rows, str(original))
        lines = csv_text.splitlines()
        assert lines[0] == "id,title,author"
        assert 'doi:1,Edited,A' in lines[1]

    def test_detects_semicolon_delimiter(self, tmp_path):
        original = tmp_path / "semi.csv"
        original.write_text("id;title;author;pub_date\ndoi:1;T;A;2020\n",
                            encoding="utf-8")
        rows = [{"id": ["doi:1", "doi:2"], "title": ["T"], "author": ["A"],
                 "pub_date": ["2020"]}]
        csv_text = CSVExporter.rows_to_csv(rows, str(original))
        assert csv_text.splitlines()[0] == "id;title;author;pub_date"
        assert "doi:1 doi:2" in csv_text  # separator inside the cell is ' '

    def test_detects_tab_delimiter(self, tmp_path):
        original = tmp_path / "tabs.csv"
        original.write_text("id\ttitle\tauthor\ndoi:1\tT\tA\n", encoding="utf-8")
        rows = [{"id": ["doi:1"], "title": ["T"], "author": ["A"]}]
        csv_text = CSVExporter.rows_to_csv(rows, str(original))
        assert csv_text.splitlines()[0] == "id\ttitle\tauthor"

    def test_values_are_csv_quoted_when_needed(self, tmp_path):
        original = tmp_path / "meta.csv"
        original.write_text("id,title,author\ndoi:1,T,A\n", encoding="utf-8")
        rows = [{"id": ["doi:1"], "title": ['Title, with comma'],
                 "author": ["A"]}]
        csv_text = CSVExporter.rows_to_csv(rows, str(original))
        assert '"Title, with comma"' in csv_text
