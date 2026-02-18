"""Service for parsing HTML and extracting table data."""
from bs4 import BeautifulSoup
from typing import Dict, List, Optional
import csv
from io import StringIO


class HTMLParser:
    """Parse HTML tables and extract data."""
    
    # Item separators for different field types
    ITEM_SEPARATORS = {
        'citing_id': ' ',
        'cited_id': ' ',
        'id': ' ',
        'author': '; ',
        'publisher': '; ',
        'editor': '; '
    }
    
    @staticmethod
    def parse_table(html_content: str) -> List[Dict[str, List[str]]]:
        """
        Parse HTML table and extract data as list of dictionaries.
        
        Args:
            html_content: HTML string containing the table
            
        Returns:
            List of dictionaries, where each dict represents a row
            with field names as keys and lists of items as values
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        
        # Get header row
        header_row = table.find('thead').find('tr')
        headers = [th.get_text(strip=True) for th in header_row.find_all('th')]
        # Skip first header (row number)
        headers = headers[1:]
        
        # Get data rows
        tbody = table.find('tbody')
        rows_data = []
        
        for row in tbody.find_all('tr'):
            cells = row.find_all('td')
            # Skip first cell (row number)
            cells = cells[1:]
            
            row_data = {}
            for header, cell in zip(headers, cells):
                # Find all item-data spans within this cell
                item_data_spans = cell.find_all('span', class_='item-data')
                
                # Extract text from each item-data span
                items = [span.get_text(strip=False) for span in item_data_spans]
                
                # If no item-data spans found, try direct text
                if not items:
                    items = [cell.get_text(strip=False)]
                
                row_data[header] = items
            
            rows_data.append(row_data)
        
        return rows_data
    
    @staticmethod
    def get_rows_by_issue(html_content: str, issue_id: str) -> List[int]:
        """
        Get row indices that contain data involved in a specific issue.
        
        Args:
            html_content: HTML string containing the table
            issue_id: ID of the issue (e.g., 'meta-0', 'cits-1')
            
        Returns:
            List of row indices (0-based)
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        
        # Find all issue icons with the given issue_id
        issue_icons = table.find_all('span', class_='issue-icon', id=issue_id)
        
        # Extract parent row indices
        row_indices = set()
        for icon in issue_icons:
            # Navigate up to the parent row
            row = icon.find_parent('tr')
            if row:
                row_id = row.get('id')
                if row_id and row_id.startswith('row'):
                    try:
                        row_idx = int(row_id.replace('row', ''))
                        row_indices.add(row_idx)
                    except ValueError:
                        pass
        
        return sorted(list(row_indices))
    
    @staticmethod
    def extract_filtered_table(html_content: str, row_indices: List[int]) -> str:
        """
        Create a new HTML table containing only specified rows.
        
        Args:
            html_content: Original HTML content
            row_indices: List of row indices to include (0-based)
            
        Returns:
            HTML string containing the filtered table
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        
        # Create a new table
        new_table = soup.new_tag('table', **{'class': table['class'], 'id': 'table-data'})
        
        # Copy header
        thead = table.find('thead')
        if thead:
            new_table.append(thead.copy())
        
        # Copy only specified rows
        tbody = table.find('tbody')
        new_tbody = soup.new_tag('tbody')
        
        row_index_set = set(row_indices)
        row_idx = 0
        
        for row in tbody.find_all('tr'):
            if row_idx in row_index_set:
                new_tbody.append(row.copy())
            row_idx += 1
        
        new_table.append(new_tbody)
        
        return str(new_table)
    
    @staticmethod
    def get_field_data_by_item_id(html_content: str, item_id: str) -> Optional[str]:
        """
        Get the current value of an item by its item_id.
        
        Args:
            html_content: HTML string containing the table
            item_id: Unique identifier for the item (e.g., '0-id-0'), which is
                     the ID of the .item-container span wrapping the .item-data span.
            
        Returns:
            Current text value or None if not found
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        container = soup.find('span', id=item_id)
        
        if not container:
            return None
        
        # item-data is a direct child of the item-container
        item_data = container.find('span', class_='item-data')
        if item_data:
            return item_data.get_text(strip=False)
        
        return None
    
    @staticmethod
    def update_item_value(html_content: str, item_id: str, new_value: str) -> str:
        """
        Update the value of an item in the HTML.
        
        Args:
            html_content: Original HTML string
            item_id: Unique identifier for the item (ID of the .item-container span)
            new_value: New text value for the item
            
        Returns:
            Updated HTML string
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        container = soup.find('span', id=item_id)
        
        if not container:
            raise ValueError(f"Item with id '{item_id}' not found")
        
        # item-data is a direct child of the item-container
        item_data = container.find('span', class_='item-data')
        if item_data:
            item_data.string = new_value
        else:
            # If no item-data child found, create one inside the container
            new_item_data = soup.new_tag('span', **{'class': 'item-data'})
            new_item_data.string = new_value
            container.insert(0, new_item_data)
        
        return str(soup)
    
    @staticmethod
    def apply_edit_tracking(html_content: str, edited_item_ids: List[str]) -> str:
        """
        Apply visual tracking (grey background) to edited items.
        
        Args:
            html_content: Original HTML string
            edited_item_ids: List of item IDs that have been edited
                             (IDs of .item-container spans)
            
        Returns:
            HTML string with edit tracking applied
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        for item_id in edited_item_ids:
            container = soup.find('span', id=item_id)
            if container:
                # item-data is a direct child of the item-container
                item_data = container.find('span', class_='item-data')
                if item_data:
                    existing_classes = item_data.get('class', [])
                    if isinstance(existing_classes, list):
                        if 'edited' not in existing_classes:
                            existing_classes.append('edited')
                        item_data['class'] = existing_classes
                    else:
                        item_data['class'] = f"{existing_classes} edited".strip()
        
        return str(soup)
    
    @staticmethod
    def remove_edit_tracking(html_content: str) -> str:
        """
        Remove visual tracking from all items.
        
        Args:
            html_content: Original HTML string
            
        Returns:
            HTML string with all edit tracking removed
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        edited_items = soup.find_all('span', class_='item-data')
        
        for item in edited_items:
            classes = item.get('class', [])
            if isinstance(classes, list):
                if 'edited' in classes:
                    classes.remove('edited')
                    item['class'] = classes if classes else None
            else:
                # Handle string class attribute
                classes_list = classes.split()
                if 'edited' in classes_list:
                    classes_list.remove('edited')
                    item['class'] = ' '.join(classes_list) if classes_list else None
        
        return str(soup)