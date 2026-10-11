"""Service for exporting HTML table data back to CSV format."""
from typing import Dict, List
import csv
from io import StringIO


class CSVExporter:
    """Export HTML table data to CSV format."""

    @staticmethod
    def generate_csv(rows_data: List[Dict[str, List[str]]]) -> str:
        """
        Generate CSV string from parsed table data (comma-delimited fallback).

        Args:
            rows_data: List of dictionaries with field names as keys
                      and lists of items as values

        Returns:
            CSV string
        """
        if not rows_data:
            return ""

        # Get field names (preserve order from first row)
        fieldnames = list(rows_data[0].keys())

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=',')

        writer.writeheader()

        for row in rows_data:
            # Join items with appropriate separator.
            # Filter out blank items first so that emptied item-containers
            # (e.g. user deleted a value) do not leave stray separators.
            csv_row = {}
            for field, items in row.items():
                if field in ['citing_id', 'cited_id', 'id']:
                    # Space-separated IDs
                    csv_row[field] = ' '.join(i for i in items if i.strip())
                elif field in ['author', 'publisher', 'editor']:
                    # Semicolon-separated agents
                    csv_row[field] = '; '.join(i for i in items if i.strip())
                else:
                    # Single value fields
                    csv_row[field] = items[0] if items else ''

            writer.writerow(csv_row)

        return output.getvalue()

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
            return CSVExporter.generate_csv(rows_data)
        
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
            # Join items with appropriate separator.
            # Filter out blank items first so that emptied item-containers
            # do not leave stray separators in the exported CSV.
            csv_row = {}
            for field in fieldnames:
                items = row.get(field, [])
                if field in ['citing_id', 'cited_id', 'id']:
                    # Space-separated IDs
                    csv_row[field] = ' '.join(i for i in items if i.strip())
                elif field in ['author', 'publisher', 'editor']:
                    # Semicolon-separated agents
                    csv_row[field] = '; '.join(i for i in items if i.strip())
                else:
                    # Single value fields
                    csv_row[field] = items[0] if items else ''
            
            writer.writerow(csv_row)
        
        return output.getvalue()