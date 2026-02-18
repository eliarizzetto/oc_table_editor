"""Service for running validation using oc_validator."""
import sys
from pathlib import Path
from typing import Optional, Tuple
import json

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

from oc_validator.main import Validator, ClosureValidator


class ValidatorService:
    """Wrapper service for oc_validator functionality."""

    @staticmethod
    def _make_no_errors_html(out_fp: str, csv_path: str) -> None:
        """
        Write a minimal 'no errors found' HTML file to out_fp.

        This is used instead of calling make_gui when the validation report is
        empty, because make_gui tries to open 'valid_page.html' via a bare
        relative path (which does not exist in the oc_table_editor working
        directory) and crashes with a FileNotFoundError.

        Args:
            out_fp:   Destination HTML file path.
            csv_path: Path to the CSV that was validated (used for the title).
        """
        filename = Path(csv_path).name
        html = (
            '<!DOCTYPE html><html lang="en"><head>'
            '<meta charset="utf-8">'
            '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">'
            '</head><body>'
            '<div class="container-fluid general-info">'
            '<h4>Validation Results</h4>'
            f'<p>There are <strong>0</strong> errors/warnings in the table submitted for validation.</p>'
            f'<p class="text-success"><strong>✓ No issues found in <em>{filename}</em>.</strong></p>'
            '</div>'
            '<div class="table-container container-fluid"></div>'
            '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>'
            '</body></html>'
        )
        with open(out_fp, 'w', encoding='utf-8') as f:
            f.write(html)

    @staticmethod
    def validate_single(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[list, str]:
        """
        Validate a single CSV file (metadata OR citations — auto-detected).

        Args:
            csv_path:            Path to the CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (error_list, report_json_path).
        """
        validator = Validator(
            csv_doc=csv_path,
            output_dir=output_dir,
            use_meta_endpoint=False,
            verify_id_existence=verify_id_existence
        )
        errors = validator.validate()
        return errors, validator.output_fp_json

    @staticmethod
    def validate_metadata(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[list, str]:
        """
        Validate a metadata CSV file.

        Args:
            csv_path:            Path to the metadata CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (error_list, report_json_path).
        """
        return ValidatorService.validate_single(csv_path, output_dir, verify_id_existence)

    @staticmethod
    def validate_citations(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> Tuple[list, str]:
        """
        Validate a citations CSV file.

        Args:
            csv_path:            Path to the citations CSV file.
            output_dir:          Directory to store validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (error_list, report_json_path).
        """
        return ValidatorService.validate_single(csv_path, output_dir, verify_id_existence)

    @staticmethod
    def validate_pair(
        meta_csv_path: str,
        cits_csv_path: str,
        meta_output_dir: str,
        cits_output_dir: str,
        verify_id_existence: bool = False
    ) -> Tuple[list, list, str, str]:
        """
        Validate paired metadata and citations CSV files using ClosureValidator.

        Args:
            meta_csv_path:       Path to the metadata CSV file.
            cits_csv_path:       Path to the citations CSV file.
            meta_output_dir:     Directory to store metadata validation output.
            cits_output_dir:     Directory to store citations validation output.
            verify_id_existence: Whether to check ID existence via external APIs.

        Returns:
            Tuple of (meta_errors, cits_errors, meta_report_json_path, cits_report_json_path).
        """
        validator = ClosureValidator(
            meta_csv_doc=meta_csv_path,
            meta_output_dir=meta_output_dir,
            cits_csv_doc=cits_csv_path,
            cits_output_dir=cits_output_dir,
            meta_kwargs={'verify_id_existence': verify_id_existence},
            cits_kwargs={'verify_id_existence': verify_id_existence}
        )
        meta_errors, cits_errors = validator.validate()
        meta_report_path = validator.meta_validator.output_fp_json
        cits_report_path = validator.cits_validator.output_fp_json
        return meta_errors, cits_errors, meta_report_path, cits_report_path
