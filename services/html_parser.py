"""Service for parsing HTML and extracting table data."""
from bs4 import BeautifulSoup, PageElement, Tag
from typing import Dict, List, Optional
import csv
from io import StringIO

from services.session_document import (
    SpliceError,
    find_row_bounds,
    insert_row_str,
)


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
                
                # Filter out blank items produced by emptied item-containers so
                # that the CSV exporter does not generate stray separators.
                items = [t for t in items if t.strip()]
                
                # If no item-data spans found, or all items were blank,
                # fall back to a single empty string (valid empty-cell CSV).
                if not items:
                    items = [cell.get_text(strip=False) if not item_data_spans
                             else '']
                
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
        from copy import copy as shallow_copy
        
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        
        # Get the class attribute safely
        table_class = table.get('class', [])
        if isinstance(table_class, list):
            class_attr = ' '.join(table_class)
        else:
            class_attr = table_class
        
        # Create a new table
        new_table = soup.new_tag('table', attrs={'class': class_attr, 'id': 'table-data'})
        
        # Copy header using BeautifulSoup's decode/encode pattern for deep copy
        thead = table.find('thead')
        if thead:
            # Parse the thead HTML to create a fresh copy
            thead_soup = BeautifulSoup(str(thead), 'html.parser')
            new_table.append(thead_soup.find('thead'))
        
        # Copy only specified rows
        tbody = table.find('tbody')
        new_tbody = soup.new_tag('tbody')
        
        row_index_set = set(row_indices)
        row_idx = 0
        
        for row in tbody.find_all('tr'):
            if row_idx in row_index_set:
                # Parse the row HTML to create a fresh copy
                row_soup = BeautifulSoup(str(row), 'html.parser')
                new_tbody.append(row_soup.find('tr'))
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
    def delete_row(html_content: str, row_id: str) -> str:
        """
        Remove an entire table row from the HTML.

        Args:
            html_content: HTML string containing the table.
            row_id:       The ``id`` attribute of the ``<tr>`` to delete
                          (e.g. ``"row5"``).

        Returns:
            Updated HTML string with the row removed.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        row = soup.find('tr', id=row_id)
        if row:
            row.decompose()
        return str(soup)

    @staticmethod
    def add_row(html_content: str) -> tuple:
        """
        Add a new empty row to the end of the table.

        The new row contains empty item-containers for each field based on
        the table structure. Field names are extracted from the header row.

        Args:
            html_content: HTML string containing the table.

        Returns:
            Tuple ``(updated_html, new_row_id)`` where ``new_row_id`` is the
            ``id`` of the newly created row (e.g., ``"row5"``).
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            return html_content, ''
        
        tbody = table.find('tbody')
        if not tbody:
            return html_content, ''
        
        # Get header row to extract field names
        thead = table.find('thead')
        if not thead:
            return html_content, ''
        
        header_row = thead.find('tr')
        if not header_row:
            return html_content, ''
        
        headers = header_row.find_all('th')
        if len(headers) < 2:  # At least row number + 1 field
            return html_content, ''
        
        # Extract field names (skip first header which is row number)
        field_names = []
        for th in headers[1:]:
            field_name = th.get_text(strip=True)
            field_names.append(field_name)
        
        # Get the row number for the new row (find highest existing row number)
        existing_rows = tbody.find_all('tr', id=True)
        if not existing_rows:
            row_number = 0
        else:
            row_numbers = []
            for row in existing_rows:
                row_id = row.get('id', '')
                if row_id.startswith('row'):
                    try:
                        row_num = int(row_id.replace('row', ''))
                        row_numbers.append(row_num)
                    except ValueError:
                        pass
            row_number = max(row_numbers) + 1 if row_numbers else 0
        
        new_row_id = f'row{row_number}'
        
        # Create new row
        new_row = soup.new_tag('tr', attrs={'id': new_row_id})
        
        # Add row-number cell
        row_number_cell = soup.new_tag('td', attrs={'class': 'row-number'})
        row_number_cell.string = str(row_number)
        new_row.append(row_number_cell)
        
        # Add cells for each field
        for idx, field_name in enumerate(field_names):
            cell = soup.new_tag('td', attrs={'class': ['field-value', field_name]})
            
            # Create empty item-container for this field
            item_container = soup.new_tag('span', attrs={'class': 'item-container', 'id': f'{row_number}-{field_name}-0'})
            item_data = soup.new_tag('span', attrs={'class': 'item-data', 'style': 'cursor: pointer;'})
            item_data.string = ''
            item_container.append(item_data)
            cell.append(item_container)
            
            new_row.append(cell)
        
        # Append new row to tbody (before any existing buttons at the end)
        tbody.append(new_row)
        
        return str(soup), new_row_id

    @staticmethod
    def clear_cell(html_content: str, row_id: str, field_name: str) -> tuple:
        """
        Clear all content from a cell, leaving exactly one empty item-container.

        Works regardless of whether the cell already has item-containers or none
        (handles both the "clear existing values" and "initialise empty cell"
        use cases).

        Args:
            html_content: HTML string containing the table.
            row_id:       ``id`` attribute of the parent ``<tr>``
                          (e.g. ``"row5"``).
            field_name:   Name of the field / column (e.g. ``"id"``, ``"author"``).

        Returns:
            Tuple ``(updated_html, new_item_id)`` where ``new_item_id`` is the
            ``id`` of the single empty item-container left in the cell.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        row = soup.find('tr', id=row_id)
        if not row:
            return html_content, ''

        # Locate the cell by its "field-value {field_name}" class pair
        cell = None
        for td in row.find_all('td', class_='field-value'):
            if field_name in td.get('class', []):
                cell = td
                break

        if not cell:
            return html_content, ''

        # Remove every existing item-container (their .sep children go with them)
        for container in cell.find_all('span', class_='item-container'):
            container.decompose()

        # Derive row number from row_id (format: "row{N}")
        row_num = row_id[3:]  # strip "row" prefix
        new_item_id = f"{row_num}-{field_name}-0"

        # Insert one fresh empty item-container so the cell remains clickable
        new_container = soup.new_tag('span', **{'class': 'item-container', 'id': new_item_id})
        new_item_data = soup.new_tag('span', **{'class': 'item-data', 'style': 'cursor: pointer;'})
        new_item_data.string = ''
        new_container.append(new_item_data)
        cell.append(new_container)

        return str(soup), new_item_id

    @staticmethod
    def remove_item(html_content: str, item_id: str) -> str:
        """
        Remove an item-container from a multi-value cell.

        The item-container is unconditionally removed from the DOM regardless of
        its position, validation status, or value content. Separator adjustments
        are handled by the frontend.

        Args:
            html_content: HTML string containing the table.
            item_id:      ID of the .item-container span to remove.

        Returns:
            Updated HTML string.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        container: Tag = soup.find('span', id=item_id)
        if not container:
            return html_content  # nothing to do

        # Unconditionally remove the entire item-container
        container.decompose()

        return str(soup)

    @staticmethod
    def get_cell_state(html_content: str, row_id: str, field_name: str) -> tuple:
        """
        Get the state of a cell in the table.

        Args:
            html_content: HTML string containing the table.
            row_id:       ``id`` attribute of the parent ``<tr>``
                          (e.g. ``"row5"``).
            field_name:   Name of the field / column (e.g. ``"id"``, ``"author"``).

        Returns:
            Tuple ``(has_value, container_count)`` where:
            - ``has_value`` is True if any item-container has non-empty content
            - ``container_count`` is the number of item-containers in the cell
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        row = soup.find('tr', id=row_id)
        if not row:
            return False, 0

        # Locate the cell by its "field-value {field_name}" class pair
        cell = None
        for td in row.find_all('td', class_='field-value'):
            if field_name in td.get('class', []):
                cell = td
                break

        if not cell:
            return False, 0

        # Get all item-containers
        containers = cell.find_all('span', class_='item-container', recursive=False)
        
        # Check if any container has a non-empty value
        has_value = False
        for container in containers:
            item_data = container.find('span', class_='item-data')
            if item_data and item_data.get_text(strip=True):
                has_value = True
                break
        
        return has_value, len(containers)

    @staticmethod
    def add_item(html_content: str, after_item_id: str, field_separator: str, value: str = '') -> tuple:
        """
        Append a new item-container to the same cell as after_item_id.

        The new container is always appended at the end of the cell.
        A .sep span (containing field_separator) is added to the
        previously-last container so that the separator appears between the old
        last value and the new item slot.

        Args:
            html_content:    HTML string containing the table.
            after_item_id:   ID of an existing .item-container in the target cell.
                             The new item is inserted after ALL existing containers
                             (append semantics regardless of which item was active).
            field_separator: The separator string to insert (e.g. ' ' or '; ').
            value:           The value to set for the new item (default: empty string).

        Returns:
            Tuple (updated_html_string, new_item_id).
            new_item_id is the id attribute assigned to the new container.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        ref_container = soup.find('span', id=after_item_id)
        if not ref_container:
            return html_content, ''

        parent = ref_container.parent
        if not parent:
            return html_content, ''

        siblings = [s for s in parent.find_all('span', class_='item-container', recursive=False)]

        # Parse row and field from the reference item-id (format: row-field-index)
        # Field name sits between the first and last '-' separated component.
        parts = after_item_id.split('-')
        # parts[0] = row number, parts[-1] = index, parts[1:-1] = field name parts
        row_part = parts[0]
        field_part = '-'.join(parts[1:-1])
        new_index = len(siblings)  # append at end
        new_item_id = f"{row_part}-{field_part}-{new_index}"

        # Add a .sep to the currently-last container (if it doesn't already have one)
        last_container = siblings[-1]
        existing_sep = last_container.find('span', class_='sep')
        if not existing_sep:
            sep_tag = soup.new_tag('span', **{'class': 'sep'})
            sep_tag.string = field_separator
            last_container.append(sep_tag)

        # Build new item-container with the provided value
        new_container = soup.new_tag('span', **{'class': 'item-container', 'id': new_item_id})
        new_item_data = soup.new_tag('span', **{'class': 'item-data', 'style': 'cursor: pointer;'})
        new_item_data.string = value
        new_container.append(new_item_data)

        # Insert after the last existing container
        last_container.insert_after(new_container)

        return str(soup), new_item_id

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
    
    @staticmethod
    def apply_added_tracking(
        html_content: str, 
        added_item_ids: List[str], 
        added_row_ids: List[str]
    ) -> str:
        """
        Apply green highlighting to added items and rows.
        
        Args:
            html_content: Original HTML string
            added_item_ids: List of item IDs that have been added
                            (IDs of .item-container spans)
            added_row_ids: List of row IDs that have been added
                           (IDs of <tr> elements)
            
        Returns:
            HTML string with added tracking applied
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Apply green background to added items
        for item_id in added_item_ids:
            container = soup.find('span', id=item_id)
            if container:
                # item-data is a direct child of the item-container
                item_data = container.find('span', class_='item-data')
                if item_data:
                    existing_classes = item_data.get('class', [])
                    if isinstance(existing_classes, list):
                        if 'added' not in existing_classes:
                            existing_classes.append('added')
                        item_data['class'] = existing_classes
                    else:
                        # Handle string class attribute
                        classes_list = existing_classes.split()
                        if 'added' not in classes_list:
                            classes_list.append('added')
                        item_data['class'] = ' '.join(classes_list)
        
        # Apply green background to added rows
        for row_id in added_row_ids:
            row = soup.find('tr', id=row_id)
            if row:
                existing_classes = row.get('class', [])
                if isinstance(existing_classes, list):
                    if 'added' not in existing_classes:
                        existing_classes.append('added')
                    row['class'] = existing_classes
                else:
                    # Handle string class attribute
                    classes_list = existing_classes.split()
                    if 'added' not in classes_list:
                        classes_list.append('added')
                    row['class'] = ' '.join(classes_list)
        
        return str(soup)
    
    @staticmethod
    def get_row_item_ids(html_content: str, row_id: str) -> List[str]:
        """
        Get all item-container IDs for a specific row.
        
        Args:
            html_content: HTML string containing the table
            row_id: Row identifier (e.g., 'row5')
            
        Returns:
            List of item-container IDs in the row
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        row = soup.find('tr', id=row_id)
        
        if not row:
            return []
        
        # Find all item-containers in this row
        item_containers = row.find_all('span', class_='item-container', recursive=True)
        
        return [container.get('id', '') for container in item_containers if container.get('id')]
    
    @staticmethod
    def get_cell_item_ids(html_content: str, row_id: str, field_name: str) -> List[str]:
        """
        Get all item-container IDs for a specific cell.
        
        Args:
            html_content: HTML string containing the table
            row_id: Row identifier (e.g., 'row5')
            field_name: Name of the field / column (e.g., 'id', 'author')
            
        Returns:
            List of item-container IDs in the cell
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        row = soup.find('tr', id=row_id)
        
        if not row:
            return []
        
        # Locate the cell by its "field-value {field_name}" class pair
        cell = None
        for td in row.find_all('td', class_='field-value'):
            if field_name in td.get('class', []):
                cell = td
                break
        
        if not cell:
            return []
        
        # Find all item-containers in this cell
        item_containers = cell.find_all('span', class_='item-container', recursive=False)
        
        return [container.get('id', '') for container in item_containers if container.get('id')]
    
    @staticmethod
    def get_all_row_ids(html_content: str) -> List[str]:
        """
        Get all row IDs from the table.
        
        Args:
            html_content: HTML string containing the table
            
        Returns:
            List of row IDs
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            return []
        
        tbody = table.find('tbody')
        if not tbody:
            return []
        
        rows = tbody.find_all('tr', id=True)
        
        return [row.get('id', '') for row in rows if row.get('id')]
    
    @staticmethod
    def identify_deletions_with_values(baseline_html: str, current_html: str) -> Dict:
        """
        Compare baseline with current HTML to identify deleted items and rows with their values.
        
        Args:
            baseline_html: Baseline HTML state (after validation)
            current_html: Current HTML state (may have deletions)
            
        Returns:
            Dictionary with 'deleted_items', 'deleted_rows', and 'deleted_item_values'
            e.g., {
                'deleted_items': ['0-id-1', '5-author-0'],
                'deleted_rows': ['row3'],
                'deleted_item_values': {'0-id-1': 'some value', '5-author-0': 'another value'}
            }
        """
        baseline_soup = BeautifulSoup(baseline_html, 'html.parser')
        current_soup = BeautifulSoup(current_html, 'html.parser')
        
        baseline_table = baseline_soup.find('table', id='table-data')
        current_table = current_soup.find('table', id='table-data')
        
        if not baseline_table or not current_table:
            return {'deleted_items': [], 'deleted_rows': [], 'deleted_item_values': {}}
        
        # Get all rows from baseline
        baseline_tbody = baseline_table.find('tbody')
        current_tbody = current_table.find('tbody')
        
        if not baseline_tbody or not current_tbody:
            return {'deleted_items': [], 'deleted_rows': [], 'deleted_item_values': {}}
        
        # Identify deleted rows
        baseline_rows = {row.get('id', '') for row in baseline_tbody.find_all('tr', id=True)}
        current_rows = {row.get('id', '') for row in current_tbody.find_all('tr', id=True)}
        
        deleted_rows = list(baseline_rows - current_rows)
        
        # Identify deleted items and capture their values
        deleted_items = []
        deleted_item_values = {}
        
        # Check each baseline row for deleted items
        for row_id in baseline_rows:
            # Skip rows that are entirely deleted
            if row_id in deleted_rows:
                continue
            
            # Find the row in both baseline and current
            baseline_row = baseline_tbody.find('tr', id=row_id)
            current_row = current_tbody.find('tr', id=row_id)
            
            if not baseline_row or not current_row:
                continue
            
            # Get all item-containers from baseline row
            baseline_items = {
                container.get('id', '') 
                for container in baseline_row.find_all('span', class_='item-container', recursive=True)
                if container.get('id')
            }
            
            # Get all item-containers from current row
            current_items = {
                container.get('id', '') 
                for container in current_row.find_all('span', class_='item-container', recursive=True)
                if container.get('id')
            }
            
            # Items in baseline but not in current are deleted
            row_deleted_items = list(baseline_items - current_items)
            deleted_items.extend(row_deleted_items)
            
            # Capture values of deleted items from baseline
            for item_id in row_deleted_items:
                container = baseline_row.find('span', id=item_id)
                if container:
                    item_data = container.find('span', class_='item-data')
                    if item_data:
                        deleted_item_values[item_id] = item_data.get_text(strip=False)
            
            # Check for value-based deletions (same ID but value cleared)
            # This handles single-value fields where clear_cell reuses the same ID
            common_items = baseline_items & current_items
            for item_id in common_items:
                baseline_container = baseline_row.find('span', id=item_id)
                current_container = current_row.find('span', id=item_id)
                
                if baseline_container and current_container:
                    baseline_data = baseline_container.find('span', class_='item-data')
                    current_data = current_container.find('span', class_='item-data')
                    
                    if baseline_data and current_data:
                        baseline_value = baseline_data.get_text(strip=False)
                        current_value = current_data.get_text(strip=False)
                        
                        # If baseline had value but current is empty → treat as deletion
                        if baseline_value.strip() and not current_value.strip():
                            deleted_items.append(item_id)
                            deleted_item_values[item_id] = baseline_value
        
        return {
            'deleted_items': deleted_items,
            'deleted_rows': deleted_rows,
            'deleted_item_values': deleted_item_values
        }
    
    @staticmethod
    def create_ghost_item_container(soup, item_id: str, value: str, is_multi_value: bool = False) -> Tag:
        """
        Create a ghost item-container element with appropriate styling.
        
        Args:
            soup: BeautifulSoup instance for creating tags
            item_id: ID for the ghost item-container
            value: Original value of the deleted item
            is_multi_value: Whether this item is part of a multi-value field
            
        Returns:
            Tag element representing the ghost item-container
        """
        ghost_container = soup.new_tag('span', attrs={
            'class': 'item-container deleted-ghost',
            'id': f'ghost-{item_id}',
            'data-ghost-item-id': item_id
        })
        
        ghost_item_data = soup.new_tag('span', attrs={
            'class': 'item-data',
            'style': 'color: #842029; font-style: italic;'
        })
        ghost_item_data.string = value
        ghost_container.append(ghost_item_data)
        
        return ghost_container
    
    @staticmethod
    def insert_deleted_overlays(
        html_content: str, 
        deletions: Dict,
        deleted_item_values: Dict[str, str]
    ) -> str:
        """
        Insert ghost overlay elements for deleted data in their original positions.
        
        Args:
            html_content: Current HTML string
            deletions: Dictionary with 'deleted_items' and 'deleted_rows' lists
            deleted_item_values: Dictionary mapping item_id to original value
            
        Returns:
            HTML string with ghost overlay elements inserted
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', id='table-data')
        
        if not table:
            return html_content
        
        tbody = table.find('tbody')
        if not tbody:
            return html_content
        
        # Get all current rows to determine insertion points
        current_rows = tbody.find_all('tr', id=True)
        current_row_indices = set()
        for row in current_rows:
            row_id = row.get('id', '')
            if row_id and row_id.startswith('row'):
                try:
                    row_idx = int(row_id.replace('row', ''))
                    current_row_indices.add(row_idx)
                except ValueError:
                    pass
        
        # Insert ghost rows for deleted rows at original positions
        for row_id in sorted(deletions.get('deleted_rows', []), 
                          key=lambda x: int(x.replace('row', '')) if x.replace('row', '').isdigit() else 0):
            row_number = int(row_id.replace('row', '')) if row_id.startswith('row') and row_id.replace('row', '').isdigit() else -1
            if row_number < 0:
                continue
            
            # Create a ghost row element
            ghost_row = soup.new_tag('tr', attrs={
                'class': 'deleted',
                'id': f'ghost-{row_id}',
                'data-ghost-row-id': row_id
            })
            
            # Add a row-number cell with the original row number
            row_number_cell = soup.new_tag('td', attrs={'class': 'row-number'})
            row_number_cell.string = str(row_number)
            ghost_row.append(row_number_cell)
            
            # Get the number of columns from the header
            thead = table.find('thead')
            if thead:
                header_row = thead.find('tr')
                if header_row:
                    num_columns = len(header_row.find_all('th'))
                    
                    # Add cells with ghost items for each field
                    for col_idx in range(1, num_columns):
                        cell = soup.new_tag('td', attrs={'class': 'field-value'})
                        
                        # Find field name from header
                        headers = header_row.find_all('th')
                        if col_idx < len(headers):
                            field_name = headers[col_idx].get_text(strip=True)
                            
                            # Look through deleted_item_values for items belonging to this row and field
                            # This handles both individual deleted items and items from fully deleted rows
                            row_num = row_id.replace('row', '')
                            found_items = False
                            
                            for item_id, value in deleted_item_values.items():
                                # Check if this item belongs to the deleted row and field
                                item_parts = item_id.split('-')
                                if len(item_parts) >= 3:
                                    item_row_num = item_parts[0]
                                    item_field = '-'.join(item_parts[1:-1])
                                    
                                    if item_row_num == row_num and item_field == field_name:
                                        # Create ghost item for this deleted item
                                        is_multi = field_name in HTMLParser.ITEM_SEPARATORS
                                        ghost_item = HTMLParser.create_ghost_item_container(
                                            soup, item_id, value, is_multi
                                        )
                                        cell.append(ghost_item)
                                        found_items = True
                            
                            if not found_items:
                                # No deleted items for this cell, show placeholder
                                ghost_cell = soup.new_tag('span', attrs={
                                    'class': 'deleted-placeholder',
                                    'style': 'color: #842029; font-style: italic;'
                                })
                                ghost_cell.string = '(deleted)'
                                cell.append(ghost_cell)
                        
                        ghost_row.append(cell)
            
            # Find insertion point - insert after the row with the highest index less than this row's index
            insertion_point = None
            for current_row in current_rows:
                current_idx = current_row.get('id', '')
                if current_idx.startswith('row') and current_idx.replace('row', '').isdigit():
                    current_num = int(current_idx.replace('row', ''))
                    if current_num > row_number:
                        break
                    insertion_point = current_row
            
            # Insert ghost row at the correct position
            if insertion_point:
                insertion_point.insert_after(ghost_row)
            else:
                # Insert at the beginning if no insertion point found
                tbody.insert(0, ghost_row)
        
        # Insert ghost items for deleted items within existing rows
        for item_id in deletions.get('deleted_items', []):
            # Parse item_id to get row_id and field_name
            parts = item_id.split('-')
            if len(parts) < 3:
                continue
            
            row_num = parts[0]
            if not row_num.isdigit():
                continue
            row_id = f'row{row_num}'
            field_name = '-'.join(parts[1:-1])
            
            # Skip if row is deleted (handled above)
            if row_id in deletions.get('deleted_rows', []):
                continue
            
            # Find the row in current HTML
            current_row = tbody.find('tr', id=row_id)
            if not current_row:
                continue
            
            # Find the cell for this field
            cell = None
            for td in current_row.find_all('td', class_='field-value'):
                if field_name in td.get('class', []):
                    cell = td
                    break
            
            if not cell:
                continue
            
            # Get value and create ghost item
            value = deleted_item_values.get(item_id, '')
            is_multi = field_name in HTMLParser.ITEM_SEPARATORS
            
            # Determine insertion point based on item index
            item_index = int(parts[-1]) if parts[-1].isdigit() else -1
            
            # Get all existing item-containers in this cell
            existing_containers = cell.find_all('span', class_='item-container', recursive=False)
            
            # Insert ghost item at original position
            ghost_item = HTMLParser.create_ghost_item_container(soup, item_id, value, is_multi)
            
            # Insert at correct position based on index
            inserted = False
            for idx, container in enumerate(existing_containers):
                container_parts = container.get('id', '').split('-')
                if len(container_parts) >= 3 and container_parts[-1].isdigit():
                    existing_idx = int(container_parts[-1])
                    if existing_idx > item_index:
                        container.insert_before(ghost_item)
                        inserted = True
                        break
            
            if not inserted:
                # Append at end if insertion point not found
                cell.append(ghost_item)

        return str(soup)

    # =====================================================================
    # Soup-scoped / string-splice variants (document-cache fast paths)
    #
    # These operate on an already-parsed tree (or on canonical strings via
    # row mini-parses) so that the DocumentCache never needs to re-parse or
    # re-serialise a whole document.  Semantics mirror the string-based
    # methods above exactly (item-id formats incl. '-empty', sep insertion,
    # 'cursor: pointer' style, class-pair cell lookup).
    # =====================================================================

    @staticmethod
    def _get_cell_in_row(row: Tag, field_name: str) -> Optional[Tag]:
        """Locate a cell by its 'field-value {field_name}' class pair."""
        for td in row.find_all('td', class_='field-value'):
            if field_name in td.get('class', []):
                return td
        return None

    @staticmethod
    def get_item_value_from_soup(soup, item_id: str) -> Optional[str]:
        """get_field_data_by_item_id, on an already-parsed tree."""
        container = soup.find('span', id=item_id)
        if not container:
            return None
        item_data = container.find('span', class_='item-data')
        return item_data.get_text(strip=False) if item_data else None

    @staticmethod
    def update_item_value_in_soup(soup, item_id: str, new_value: str) -> None:
        """update_item_value, on an already-parsed tree (in place)."""
        container = soup.find('span', id=item_id)
        if not container:
            raise ValueError(f"Item with id '{item_id}' not found")
        item_data = container.find('span', class_='item-data')
        if item_data:
            item_data.string = new_value
        else:
            new_item_data = soup.new_tag('span', **{'class': 'item-data'})
            new_item_data.string = new_value
            container.insert(0, new_item_data)

    @staticmethod
    def remove_item_in_soup(soup, item_id: str) -> None:
        """remove_item, on an already-parsed tree (in place)."""
        container = soup.find('span', id=item_id)
        if container:
            container.decompose()

    @staticmethod
    def get_cell_state_in_row(row: Tag, field_name: str) -> tuple:
        """get_cell_state, scoped to an already-found row Tag."""
        cell = HTMLParser._get_cell_in_row(row, field_name)
        if not cell:
            return False, 0
        containers = cell.find_all('span', class_='item-container', recursive=False)
        has_value = False
        for container in containers:
            item_data = container.find('span', class_='item-data')
            if item_data and item_data.get_text(strip=True):
                has_value = True
                break
        return has_value, len(containers)

    @staticmethod
    def clear_cell_in_soup(soup, row: Tag, field_name: str) -> str:
        """clear_cell, on an already-parsed tree.  Returns the new item id."""
        cell = HTMLParser._get_cell_in_row(row, field_name)
        if not cell:
            return ''
        for container in cell.find_all('span', class_='item-container'):
            container.decompose()
        row_num = (row.get('id') or 'row0')[3:]
        new_item_id = f"{row_num}-{field_name}-0"
        new_container = soup.new_tag('span', **{'class': 'item-container', 'id': new_item_id})
        new_item_data = soup.new_tag('span', **{'class': 'item-data', 'style': 'cursor: pointer;'})
        new_item_data.string = ''
        new_container.append(new_item_data)
        cell.append(new_container)
        return new_item_id

    @staticmethod
    def add_item_in_cell(soup, row: Tag, field_name: str, value: str = '') -> str:
        """add_item, located by row/field instead of a reference item id.

        Fixes the duplicate-id bug of the string-based ``add_item`` (which
        used ``len(siblings)`` as the new index — after deleting a middle
        item this collides with an existing id).  The new index is
        ``max(existing numeric indices) + 1`` ('-empty' suffixes skipped).
        """
        cell = HTMLParser._get_cell_in_row(row, field_name)
        if not cell:
            raise ValueError(f"Field '{field_name}' not found in row")
        containers = cell.find_all('span', class_='item-container', recursive=False)
        indices = []
        for c in containers:
            cparts = (c.get('id') or '').split('-')
            if len(cparts) >= 3 and cparts[-1].isdigit():
                indices.append(int(cparts[-1]))
        new_index = (max(indices) + 1) if indices else 0
        row_part = (row.get('id') or 'row0')[3:]
        new_item_id = f"{row_part}-{field_name}-{new_index}"

        new_container = soup.new_tag('span', **{'class': 'item-container', 'id': new_item_id})
        new_item_data = soup.new_tag('span', **{'class': 'item-data', 'style': 'cursor: pointer;'})
        new_item_data.string = value
        new_container.append(new_item_data)

        if containers:
            last_container = containers[-1]
            if not last_container.find('span', class_='sep'):
                sep_tag = soup.new_tag('span', **{'class': 'sep'})
                sep_tag.string = HTMLParser.ITEM_SEPARATORS.get(field_name, '')
                last_container.append(sep_tag)
            last_container.insert_after(new_container)
        else:
            cell.append(new_container)
        return new_item_id

    @staticmethod
    def add_row_to_soup(soup) -> str:
        """add_row, on an already-parsed tree.  Returns the new row id."""
        table = soup.find('table', id='table-data')
        if not table:
            return ''
        tbody = table.find('tbody')
        thead = table.find('thead')
        if not tbody or not thead:
            return ''
        header_row = thead.find('tr')
        if not header_row:
            return ''
        headers = header_row.find_all('th')
        if len(headers) < 2:
            return ''
        field_names = [th.get_text(strip=True) for th in headers[1:]]

        row_numbers = []
        for row in tbody.find_all('tr', id=True):
            row_id = row.get('id', '')
            if row_id.startswith('row'):
                try:
                    row_numbers.append(int(row_id.replace('row', '')))
                except ValueError:
                    pass
        row_number = max(row_numbers) + 1 if row_numbers else 0
        new_row_id = f'row{row_number}'

        new_row = soup.new_tag('tr', attrs={'id': new_row_id})
        row_number_cell = soup.new_tag('td', attrs={'class': 'row-number'})
        row_number_cell.string = str(row_number)
        new_row.append(row_number_cell)
        for field_name in field_names:
            cell = soup.new_tag('td', attrs={'class': ['field-value', field_name]})
            item_container = soup.new_tag('span', attrs={'class': 'item-container', 'id': f'{row_number}-{field_name}-0'})
            item_data = soup.new_tag('span', attrs={'class': 'item-data', 'style': 'cursor: pointer;'})
            item_data.string = ''
            item_container.append(item_data)
            cell.append(item_container)
            new_row.append(cell)
        tbody.append(new_row)
        return new_row_id

    @staticmethod
    def delete_row_in_soup(soup, row_id: str) -> bool:
        """delete_row, on an already-parsed tree.  Returns True if removed."""
        row = soup.find('tr', id=row_id)
        if row:
            row.decompose()
            return True
        return False

    @staticmethod
    def get_row_items_with_values(row: Tag) -> Dict[str, str]:
        """All item ids -> values of a row in a single pass (ghost tracking)."""
        result: Dict[str, str] = {}
        for container in row.find_all('span', class_='item-container', recursive=True):
            cid = container.get('id', '')
            if not cid:
                continue
            item_data = container.find('span', class_='item-data')
            if item_data is not None:
                result[cid] = item_data.get_text(strip=False)
        return result

    @staticmethod
    def get_cell_items_with_values(row: Tag, field_name: str) -> Dict[str, str]:
        """All item ids -> values of one cell in a single pass."""
        cell = HTMLParser._get_cell_in_row(row, field_name)
        if not cell:
            return {}
        result: Dict[str, str] = {}
        for container in cell.find_all('span', class_='item-container', recursive=False):
            cid = container.get('id', '')
            if not cid:
                continue
            item_data = container.find('span', class_='item-data')
            if item_data is not None:
                result[cid] = item_data.get_text(strip=False)
        return result

    @staticmethod
    def get_rows_by_issue_in_soup(soup, issue_id: str) -> List[int]:
        """get_rows_by_issue, on an already-parsed tree."""
        table = soup.find('table', id='table-data')
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        row_indices = set()
        for icon in table.find_all('span', class_='issue-icon', id=issue_id):
            row = icon.find_parent('tr')
            if row:
                row_id = row.get('id')
                if row_id and row_id.startswith('row'):
                    try:
                        row_indices.add(int(row_id.replace('row', '')))
                    except ValueError:
                        pass
        return sorted(row_indices)

    @staticmethod
    def build_filtered_table_html(soup, row_indices: List[int]) -> str:
        """extract_filtered_table without re-parsing: concatenates the kept
        rows' serializations from the cached tree."""
        table = soup.find('table', id='table-data')
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        table_class = table.get('class', [])
        class_attr = ' '.join(table_class) if isinstance(table_class, list) else table_class
        parts = [f'<table class="{class_attr}" id="table-data">']
        thead = table.find('thead')
        if thead:
            parts.append(str(thead))
        parts.append('<tbody>')
        row_index_set = set(row_indices)
        for row_idx, row in enumerate(table.find('tbody').find_all('tr')):
            if row_idx in row_index_set:
                parts.append(str(row))
        parts.append('</tbody></table>')
        return ''.join(parts)

    @staticmethod
    def parse_table_from_soup(soup) -> List[Dict[str, List[str]]]:
        """parse_table, on an already-parsed tree."""
        table = soup.find('table', id='table-data')
        if not table:
            raise ValueError("Table with id 'table-data' not found in HTML")
        header_row = table.find('thead').find('tr')
        headers = [th.get_text(strip=True) for th in header_row.find_all('th')][1:]
        rows_data: List[Dict[str, List[str]]] = []
        for row in table.find('tbody').find_all('tr'):
            cells = row.find_all('td')[1:]
            row_data = {}
            for header, cell in zip(headers, cells):
                item_data_spans = cell.find_all('span', class_='item-data')
                items = [span.get_text(strip=False) for span in item_data_spans]
                items = [t for t in items if t.strip()]
                if not items:
                    items = [cell.get_text(strip=False) if not item_data_spans else '']
                row_data[header] = items
            rows_data.append(row_data)
        return rows_data

    @staticmethod
    def apply_tracking_to_row_html(row_html: str, edited_item_ids: List[str],
                                   added_item_ids: List[str], added_row: bool) -> str:
        """Apply edited/added tracking classes to a single row string.

        Mini-parses the row (~1 ms), mirrors the class-append logic of
        apply_edit_tracking / apply_added_tracking, and re-serialises.
        """
        edited = set(edited_item_ids)
        added = set(added_item_ids)
        bs = BeautifulSoup(row_html, 'html.parser')
        row = bs.find('tr')
        if row is None:
            return row_html
        for container in row.find_all('span', class_='item-container'):
            cid = container.get('id', '')
            if cid in edited or cid in added:
                item_data = container.find('span', class_='item-data')
                if item_data:
                    classes = item_data.get('class', [])
                    if isinstance(classes, list):
                        if cid in edited and 'edited' not in classes:
                            classes.append('edited')
                        if cid in added and 'added' not in classes:
                            classes.append('added')
                        item_data['class'] = classes
                    else:
                        classes_list = classes.split()
                        if cid in edited and 'edited' not in classes_list:
                            classes_list.append('edited')
                        if cid in added and 'added' not in classes_list:
                            classes_list.append('added')
                        item_data['class'] = ' '.join(classes_list)
        if added_row:
            classes = row.get('class', [])
            if isinstance(classes, list):
                if 'added' not in classes:
                    classes.append('added')
                row['class'] = classes
            else:
                classes_list = classes.split()
                if 'added' not in classes_list:
                    classes_list.append('added')
                row['class'] = ' '.join(classes_list)
        return str(row)

    @staticmethod
    def identify_deletions_fast(baseline_soup, current_soup) -> Dict:
        """identify_deletions_with_values with O(rows + items) dict indexes
        instead of O(rows²) per-row ``find`` scans."""
        baseline_table = baseline_soup.find('table', id='table-data')
        current_table = current_soup.find('table', id='table-data')
        if not baseline_table or not current_table:
            return {'deleted_items': [], 'deleted_rows': [], 'deleted_item_values': {}}
        baseline_tbody = baseline_table.find('tbody')
        current_tbody = current_table.find('tbody')
        if not baseline_tbody or not current_tbody:
            return {'deleted_items': [], 'deleted_rows': [], 'deleted_item_values': {}}

        baseline_rows = {tr.get('id'): tr for tr in baseline_tbody.find_all('tr', id=True)}
        current_rows = {tr.get('id'): tr for tr in current_tbody.find_all('tr', id=True)}
        deleted_rows = list(baseline_rows.keys() - current_rows.keys())

        deleted_items: List[str] = []
        deleted_item_values: Dict[str, str] = {}

        def _items_by_id(row: Tag) -> Dict[str, Tag]:
            return {
                c.get('id'): c
                for c in row.find_all('span', class_='item-container', recursive=True)
                if c.get('id')
            }

        for row_id, baseline_row in baseline_rows.items():
            if row_id in current_rows:
                current_row = current_rows[row_id]
                baseline_items = _items_by_id(baseline_row)
                current_items = _items_by_id(current_row)
                for item_id, b_container in baseline_items.items():
                    b_data = b_container.find('span', class_='item-data')
                    b_value = b_data.get_text(strip=False) if b_data else ''
                    if item_id not in current_items:
                        deleted_items.append(item_id)
                        deleted_item_values[item_id] = b_value
                    else:
                        c_data = current_items[item_id].find('span', class_='item-data')
                        c_value = c_data.get_text(strip=False) if c_data else ''
                        if b_value.strip() and not c_value.strip():
                            deleted_items.append(item_id)
                            deleted_item_values[item_id] = b_value

        return {
            'deleted_items': deleted_items,
            'deleted_rows': deleted_rows,
            'deleted_item_values': deleted_item_values
        }

    @staticmethod
    def _build_ghost_row_html(current_soup, row_id: str,
                              deleted_item_values: Dict[str, str]) -> str:
        """Build the ghost <tr> string for a fully deleted row (mirrors the
        ghost-row branch of insert_deleted_overlays)."""
        row_number = (int(row_id.replace('row', ''))
                      if row_id.startswith('row') and row_id.replace('row', '').isdigit()
                      else -1)
        bs = BeautifulSoup('', 'html.parser')
        ghost_row = bs.new_tag('tr', attrs={
            'class': 'deleted',
            'id': f'ghost-{row_id}',
            'data-ghost-row-id': row_id
        })
        row_number_cell = bs.new_tag('td', attrs={'class': 'row-number'})
        row_number_cell.string = str(row_number)
        ghost_row.append(row_number_cell)

        table = current_soup.find('table', id='table-data')
        header_row = table.find('thead').find('tr') if table and table.find('thead') else None
        headers = header_row.find_all('th') if header_row else []
        row_num = row_id.replace('row', '')

        for col_idx in range(1, len(headers)):
            cell = bs.new_tag('td', attrs={'class': 'field-value'})
            field_name = headers[col_idx].get_text(strip=True)
            found_items = False
            for item_id, value in deleted_item_values.items():
                item_parts = item_id.split('-')
                if len(item_parts) >= 3:
                    if item_parts[0] == row_num and '-'.join(item_parts[1:-1]) == field_name:
                        ghost_item = HTMLParser.create_ghost_item_container(
                            bs, item_id, value, field_name in HTMLParser.ITEM_SEPARATORS
                        )
                        cell.append(ghost_item)
                        found_items = True
            if not found_items:
                ghost_cell = bs.new_tag('span', attrs={
                    'class': 'deleted-placeholder',
                    'style': 'color: #842029; font-style: italic;'
                })
                ghost_cell.string = '(deleted)'
                cell.append(ghost_cell)
            ghost_row.append(cell)
        return str(ghost_row)

    @staticmethod
    def insert_deleted_overlays_fast(current_html: str, current_soup,
                                     deletions: Dict,
                                     deleted_item_values: Dict[str, str]) -> str:
        """insert_deleted_overlays as string surgery on the canonical HTML.

        Ghost rows/items are spliced into a *copy* of the canonical string via
        per-row mini-parses, so the cached tree and canonical state are never
        mutated by a view.
        """
        table = current_soup.find('table', id='table-data')
        if not table:
            return current_html
        html_out = current_html

        # Current rows in document order, for ghost-row insertion anchors
        rows_in_order = []
        for tr in table.find('tbody').find_all('tr', id=True):
            rid = tr.get('id', '')
            if rid.startswith('row') and rid.replace('row', '').isdigit():
                rows_in_order.append((rid, int(rid.replace('row', ''))))

        deleted_rows = deletions.get('deleted_rows', [])
        for row_id in sorted(
            deleted_rows,
            key=lambda x: int(x.replace('row', '')) if x.replace('row', '').isdigit() else 0
        ):
            number = (int(row_id.replace('row', ''))
                      if row_id.startswith('row') and row_id.replace('row', '').isdigit()
                      else -1)
            if number < 0:
                continue
            ghost_html = HTMLParser._build_ghost_row_html(current_soup, row_id, deleted_item_values)
            anchor_id = next((rid for rid, num in rows_in_order if num > number), None)
            html_out = insert_row_str(html_out, ghost_html, anchor_id)

        for item_id in deletions.get('deleted_items', []):
            parts = item_id.split('-')
            if len(parts) < 3:
                continue
            row_num = parts[0]
            if not row_num.isdigit():
                continue
            row_id = f'row{row_num}'
            field_name = '-'.join(parts[1:-1])
            if row_id in deleted_rows:
                continue
            try:
                start, end = find_row_bounds(html_out, row_id)
            except SpliceError:
                continue
            bs = BeautifulSoup(html_out[start:end], 'html.parser')
            row = bs.find('tr')
            if row is None:
                continue
            cell = HTMLParser._get_cell_in_row(row, field_name)
            if not cell:
                continue
            value = deleted_item_values.get(item_id, '')
            ghost_item = HTMLParser.create_ghost_item_container(
                bs, item_id, value, field_name in HTMLParser.ITEM_SEPARATORS
            )
            item_index = int(parts[-1]) if parts[-1].isdigit() else -1
            inserted = False
            for container in cell.find_all('span', class_='item-container', recursive=False):
                container_parts = container.get('id', '').split('-')
                if len(container_parts) >= 3 and container_parts[-1].isdigit():
                    if int(container_parts[-1]) > item_index:
                        container.insert_before(ghost_item)
                        inserted = True
                        break
            if not inserted:
                cell.append(ghost_item)
            html_out = html_out[:start] + str(row) + html_out[end:]

        return html_out
