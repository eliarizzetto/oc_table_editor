"""Service for exporting HTML table data back to CSV format."""
from typing import Dict, List
import csv
from io import StringIO
from oc_validator.helper import read_csv


class CSVExporter:
    """Export HTML table data to CSV format."""
    
    @staticmethod
    def generate_csv(rows_data: List[Dict[str, List[str]]], table_type: str) -> str:
        """
        Generate CSV string from parsed table data.
        
        Args:
            rows_data: List of dictionaries with field names as keys
                      and lists of items as values
            table_type: 'meta' or 'cits'
            
        Returns:
            CSV string
        """
        if not rows_data:
            return ""
        
        # Get field names (preserve order from first row)
        fieldnames = list(rows_data[0].keys())
        
        # Detect CSV delimiter by checking original file structure
        # For now, use comma as default
        delimiter = ','
        
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter)
        
        writer.writeheader()
        
        for row in rows_data:
            # Join items with appropriate separator
            csv_row = {}
            for field, items in row.items():
                if field in ['citing_id', 'cited_id', 'id']:
                    # Space-separated IDs
                    csv_row[field] = ' '.join(items)
                elif field in ['author', 'publisher', 'editor']:
                    # Semicolon-separated agents
                    csv_row[field] = '; '.join(items)
                else:
                    # Single value fields
                    csv_row[field] = items[0] if items else ''
            
            writer.writerow(csv_row)
        
        return output.getvalue()
    
    @staticmethod
    def get_delimiter(csv_content: str) -> str:
        """
        Detect CSV delimiter from content.
        
        Args:
            csv_content: CSV string content
            
        Returns:
            Detected delimiter (comma, semicolon, or tab)
        """
        # Try to detect delimiter
        for delimiter in [',', ';', '\t']:
            # Read first line and count delimiter occurrences
            first_line = csv_content.split('\n')[0]
            if first_line.count(delimiter) >= 1:
                return delimiter
        
        # Default to comma
        return ','
    
    @staticmethod
    def rows_to_csv(rows_data: List[Dict[str, List[str]]], original_csv_path: str) -> str:
        """
        Generate CSV string using delimiter from original file.
        
        Args:
            rows_data: List of dictionaries with field names as keys
                      and lists of items as values
            original_csv_path: Path to original CSV file to detect delimiter
            
        Returns:
            CSV string
        """
        if not rows_data:
            return ""
        
        # Read original file to detect delimiter
        from oc_validator.helper import read_csv
        original_data = read_csv(original_csv_path)
        
        if not original_data:
            # Fallback to default
            return CSVExporter.generate_csv(rows_data, 'meta')
        
        # Get delimiter from original file
        first_row_keys = list(original_data[0].keys())
        fieldnames = first_row_keys
        
        # Determine delimiter by checking file content
        with open(original_csv_path, 'r', encoding='utf-8') as f:
            first_line = f.readline()
            for delimiter in [',', ';', '\t']:
                if first_line.count(delimiter) >= len(fieldnames) - 1:
                    break
            else:
                delimiter = ','
        
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter)
        
        writer.writeheader()
        
        for row in rows_data:
            # Join items with appropriate separator
            csv_row = {}
            for field in fieldnames:
                items = row.get(field, [])
                if field in ['citing_id', 'cited_id', 'id']:
                    # Space-separated IDs
                    csv_row[field] = ' '.join(items)
                elif field in ['author', 'publisher', 'editor']:
                    # Semicolon-separated agents
                    csv_row[field] = '; '.join(items)
                else:
                    # Single value fields
                    csv_row[field] = items[0] if items else ''
            
            writer.writerow(csv_row)
        
        return output.getvalue()