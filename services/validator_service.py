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
        verify_id_existence: bool = False
    ) -> Tuple[list, list]:
        """
        Validate paired metadata and citations CSV files.
        
        Args:
            meta_csv_path: Path to the metadata CSV file
            cits_csv_path: Path to the citations CSV file
            meta_output_dir: Directory to store metadata validation output
            cits_output_dir: Directory to store citations validation output
            verify_id_existence: Whether to check ID existence (external APIs)
            
        Returns:
            Tuple of (metadata_errors, citations_errors)
        """
        validator = ClosureValidator(
            meta_csv_doc=meta_csv_path,
            meta_output_dir=meta_output_dir,
            cits_csv_doc=cits_csv_path,
            cits_output_dir=cits_output_dir,
            meta_kwargs={'verify_id_existence': verify_id_existence},
            cits_kwargs={'verify_id_existence': verify_id_existence}
        )
        return validator.validate()
    
    @staticmethod
    def get_report_json_path(csv_path: str, output_dir: str) -> Optional[str]:
        """
        Get path to JSON validation report.
        
        Args:
            csv_path: Original CSV file path
            output_dir: Output directory
            
        Returns:
            Path to JSON report or None if not found
        """
        from os import listdir
        basename = Path(csv_path).stem
        output_path = Path(output_dir)
        
        # Look for JSON report with multiple possible patterns
        json_files = [f for f in listdir(output_dir) if f.endswith('.json')]
        
        # Skip edit_state.json as it's our internal tracking file
        for f in json_files:
            f_lower = f.lower()
            # Try different naming patterns, but skip edit_state.json
            if f_lower.endswith('.json') and 'edit_state' not in f_lower and basename in f_lower:
                return str(output_path / f)
        
        # If not found, try to find any JSON file except edit_state.json
        for f in json_files:
            if f != 'edit_state.json':
                return str(output_path / f)
        
        return None
