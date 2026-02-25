"""Service for parsing HTML and extracting table data."""
from bs4 import BeautifulSoup, PageElement, Tag
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
            
            # Add a row-number cell
            row_number_cell = soup.new_tag('td', attrs={'class': 'row-number'})
            row_number_cell.string = '×'  # Mark as deleted
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
                            
                            # Check if any deleted items belong to this row and field
                            for item_id in deletions.get('deleted_items', []):
                                if item_id.startswith(row_id.replace('row', '')):
                                    item_parts = item_id.split('-')
                                    if len(item_parts) >= 3:
                                        item_field = '-'.join(item_parts[1:-1])
                                        if item_field == field_name:
                                            # Create ghost item for this deleted item
                                            value = deleted_item_values.get(item_id, '')
                                            is_multi = field_name in HTMLParser.ITEM_SEPARATORS
                                            ghost_item = HTMLParser.create_ghost_item_container(
                                                soup, item_id, value, is_multi
                                            )
                                            cell.append(ghost_item)
                            
                            if not cell.find('span', class_='item-container'):
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
