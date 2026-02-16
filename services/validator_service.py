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
    def validate_metadata(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> list:
        """
        Validate a metadata CSV file.
        
        Args:
            csv_path: Path to the metadata CSV file
            output_dir: Directory to store validation output
            verify_id_existence: Whether to check ID existence (external APIs)
            
        Returns:
            List of validation errors
        """
        validator = Validator(
            csv_doc=csv_path,
            output_dir=output_dir,
            use_meta_endpoint=False,
            verify_id_existence=verify_id_existence
        )
        return validator.validate()
    
    @staticmethod
    def validate_citations(csv_path: str, output_dir: str, verify_id_existence: bool = False) -> list:
        """
        Validate a citations CSV file.
        
        Args:
            csv_path: Path to the citations CSV file
            output_dir: Directory to store validation output
            verify_id_existence: Whether to check ID existence (external APIs)
            
        Returns:
            List of validation errors
        """
        validator = Validator(
            csv_doc=csv_path,
            output_dir=output_dir,
            use_meta_endpoint=False,
            verify_id_existence=verify_id_existence
        )
        return validator.validate()
    
    @staticmethod
    def validate_pair(
        meta_csv_path: str,
        cits_csv_path: str,
        meta_output_dir: str,
        cits_output_dir: str,
        verify_id_existence: bool = False,
        strict_sequentiality: bool = False
    ) -> Tuple[list, list]:
        """
        Validate paired metadata and citations CSV files.
        
        Args:
            meta_csv_path: Path to the metadata CSV file
            cits_csv_path: Path to the citations CSV file
            meta_output_dir: Directory to store metadata validation output
            cits_output_dir: Directory to store citations validation output
            verify_id_existence: Whether to check ID existence (external APIs)
            strict_sequentiality: If True, skip closure check if other checks fail
            
        Returns:
            Tuple of (metadata_errors, citations_errors)
        """
        validator = ClosureValidator(
            meta_csv_doc=meta_csv_path,
            meta_output_dir=meta_output_dir,
            cits_csv_doc=cits_csv_path,
            cits_output_dir=cits_output_dir,
            strict_sequentiality=strict_sequentiality,
            meta_kwargs={'verify_id_existence': verify_id_existence},
            cits_kwargs={'verify_id_existence': verify_id_existence}
        )
        return validator.validate()
    
    @staticmethod
    def get_report_json_path(csv_path: str, output_dir: str) -> Optional[str]:
        """
        Get the path to the JSON validation report.
        
        Args:
            csv_path: Original CSV file path
            output_dir: Output directory
            
        Returns:
            Path to JSON report or None if not found
        """
        from os.path import listdir
        basename = Path(csv_path).stem
        
        # Look for the JSON report
        for f in listdir(output_dir):
            if f.startswith(f"out_validate_{basename}") or f.startswith(f"out_validate_{basename}_"):
                return str(Path(output_dir) / f)
        
        return None