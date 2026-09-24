"""Service for running validation using oc_validator."""
import json
from os.path import abspath, dirname, join
from pathlib import Path
from typing import Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from oc_validator.helper import CSVStreamReader
from oc_validator.interface import gui as oc_gui
from oc_validator.main import Validator, ClosureValidator
from oc_validator.table_reader import CitationsRow, MetadataRow

from services.html_parser import HTMLParser  # noqa: used for ITEM_SEPARATORS


def load_jsonl_report(jsonl_path: str) -> list[dict]:
    """
    Load a JSON-Lines validation report into a single in-memory list.

    Reads each line of the ``.jsonl`` file produced by ``oc_validator`` and
    returns the collected error dictionaries.

    Args:
        jsonl_path: Path to the ``.jsonl`` validation report file.

    Returns:
        A list of error dictionaries.  Empty if the file does not exist or
        contains no valid JSON lines.
    """
    errors: list[dict] = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                errors.append(json.loads(line))
    return errors


class ValidatorService:
    """Wrapper service for oc_validator functionality."""

    @staticmethod
    def _make_no_errors_html(out_fp: str, csv_path: str,
                             table_label: str = 'Metadata') -> None:
        """
        Write a minimal 'no errors found' HTML file (no table) to out_fp.

        Fallback for valid CSVs with **no data rows**: ``make_gui``'s
        template needs at least one row to render the header (``data[0]``).
        Valid tables WITH data rows get a real editable table from
        :meth:`make_valid_table_html` instead.

        Args:
            out_fp:      Destination HTML file path.
            csv_path:    Path to the CSV that was validated (used for the title).
            table_label: Table type shown in the header ('Metadata'/'Citations').
        """
        filename = Path(csv_path).name
        try:
            total_rows = sum(1 for _ in CSVStreamReader(csv_path))
        except Exception:
            total_rows = 0
        html = (
            '<!DOCTYPE html><html lang="en"><head>'
            '<meta charset="utf-8">'
            '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">'
            '</head><body>'
            '<div class="container-fluid general-info table-stats">'
            f'<h4>{table_label}</h4>'
            f'<p class="table-stats-line">Errors: 0 | Warnings: 0 | Invalid rows: 0 | Total rows: {total_rows}</p>'
            f'<p class="text-success mb-0"><strong>✓ No issues found in <em>{filename}</em>.</strong></p>'
            '</div>'
            '<div class="table-container container-fluid"></div>'
            '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>'
            '</body></html>'
        )
        with open(out_fp, 'w', encoding='utf-8') as f:
            f.write(html)

    @staticmethod
    def make_valid_table_html(out_fp: str, csv_path: str,
                              table_label: str = 'Metadata') -> None:
        """
        Write an HTML visualisation of a VALID (empty-report) CSV table.

        Renders the very same ``invalid_template.j2`` ``make_gui`` uses —
        with zero issues — so the output is byte-compatible with the
        invalid-table contract the editor consumes (row ids, item ids,
        item-container/item-data markup, separators): a valid table stays
        fully editable.  ``make_gui`` itself cannot do this: on an empty
        report it short-circuits to ``valid_page.html`` via a bare relative
        path that does not exist in this project's working directory and
        crashes with a FileNotFoundError.

        ``table_label`` is only used by the no-data-rows fallback
        (``_make_no_errors_html``).

        Args:
            out_fp:      Destination HTML file path.
            csv_path:    Path to the CSV that was validated.
            table_label: Table type shown in the fallback header.
        """
        csv_stream = CSVStreamReader(csv_fp=csv_path)
        first_row = None
        for row in csv_stream:
            first_row = row
            break
        if first_row is None:
            ValidatorService._make_no_errors_html(out_fp, csv_path, table_label)
            return

        # Same table-type detection rule as make_gui.
        table_type = 'meta' if len(list(first_row.keys())) > 4 else 'cits'
        parser = MetadataRow if table_type == 'meta' else CitationsRow
        structured_data = [parser(row) for row in csv_stream.stream()]

        # An empty report leaves every item's issues list empty: the table
        # renders with the full item markup but no issue icons.
        mapped_data, mapped_errors = oc_gui.map_errors_to_data(
            structured_data, [])

        current_dir = dirname(abspath(oc_gui.__file__))
        env = Environment(loader=FileSystemLoader(current_dir),
                          trim_blocks=True, lstrip_blocks=True)
        template = env.get_template('invalid_template.j2')
        with open(join(current_dir, 'script.js'), 'r', encoding='utf-8') as sf, \
                open(join(current_dir, 'style.css'), 'r', encoding='utf-8') as stf:
            script = sf.read()
            style = stf.read()

        html_output = template.render(
            error_count=0,
            data=mapped_data,
            errors=mapped_errors,
            item_separators=HTMLParser.ITEM_SEPARATORS,  # same map as make_gui's
            script=script,
            style=style,
        )
        with open(out_fp, 'w', encoding='utf-8') as f:
            f.write(html_output)

    @staticmethod
    def validate_single(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[bool, str]:
        """
        Validate a single CSV file (metadata OR citations — auto-detected).

        Args:
            csv_path:            Path to the CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (is_valid, report_jsonl_path).
        """
        validator = Validator(
            csv_doc=csv_path,
            output_dir=output_dir,
            use_meta_endpoint=False,
            verify_id_existence=verify_id_existence
        )
        is_valid = validator.validate()
        return is_valid, validator.output_fp_json

    @staticmethod
    def validate_metadata(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[bool, str]:
        """
        Validate a metadata CSV file.

        Args:
            csv_path:            Path to the metadata CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (is_valid, report_jsonl_path).
        """
        return ValidatorService.validate_single(csv_path, output_dir, verify_id_existence)

    @staticmethod
    def validate_citations(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[bool, str]:
        """
        Validate a citations CSV file.

        Args:
            csv_path:            Path to the citations CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (is_valid, report_jsonl_path).
        """
        return ValidatorService.validate_single(csv_path, output_dir, verify_id_existence)

    @staticmethod
    def validate_pair(
        meta_csv_path: str,
        cits_csv_path: str,
        meta_output_dir: str,
        cits_output_dir: str,
        verify_id_existence: bool = False
    ) -> Tuple[bool, bool, str, str]:
        """
        Validate paired metadata and citations CSV files using ClosureValidator.

        Args:
            meta_csv_path:       Path to the metadata CSV file.
            cits_csv_path:       Path to the citations CSV file.
            meta_output_dir:     Directory to store metadata validation output.
            cits_output_dir:     Directory to store citations validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (meta_is_valid, cits_is_valid, meta_report_jsonl_path, cits_report_jsonl_path).
        """
        validator = ClosureValidator(
            meta_in=meta_csv_path,
            meta_out_dir=meta_output_dir,
            cits_in=cits_csv_path,
            cits_out_dir=cits_output_dir,
            meta_kwargs={'verify_id_existence': verify_id_existence},
            cits_kwargs={'verify_id_existence': verify_id_existence}
        )
        meta_is_valid, cits_is_valid = validator.validate()
        meta_report_path = validator.meta_validator.output_fp_json
        cits_report_path = validator.cits_validator.output_fp_json
        return meta_is_valid, cits_is_valid, meta_report_path, cits_report_path
