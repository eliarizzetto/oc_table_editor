"""Row-level DOM surgery for HTMLParser table rows.

Since the event-sourced-journal refactor these helpers operate on *row
mini-soups* (a single ``<tr>`` parsed with BeautifulSoup, ~1 ms per row) —
the replay engine in ``services/view_builder.py`` calls them to apply
journal events, and the mutation routes call them to validate/inspect the
current state of a row.

Semantics are contracted with ``oc_validator``'s ``make_gui`` output:
- item ids: ``{row}-{field}-{idx}`` (``{row}-{field}-empty`` for empty
  fields at generation time);
- cells: ``<td class="field-value {field}">``;
- items: ``<span class="item-container"><span class="item-data">…</span>
  <span class="issue-icon …"/>…<span class="sep">…</span></span>``;
- separators per ``ITEM_SEPARATORS`` (must stay in sync with
  ``oc_validator.interface.gui.make_gui`` and the frontend's
  ``MULTI_VALUE_FIELDS``).
"""
from bs4 import BeautifulSoup, Tag
from typing import Dict, List, Optional


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

    # =====================================================================
    # Row-scoped helpers (operate on mini-parsed <tr> soups)
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
        """Current text of an item's ``.item-data`` span, or None if absent."""
        container = soup.find('span', id=item_id)
        if not container:
            return None
        item_data = container.find('span', class_='item-data')
        return item_data.get_text(strip=False) if item_data else None

    @staticmethod
    def update_item_value_in_soup(soup, item_id: str, new_value: str) -> None:
        """Set an item's value in place (creates the .item-data if missing)."""
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
        """Remove an item-container (its issue icons and sep go with it)."""
        container = soup.find('span', id=item_id)
        if container:
            container.decompose()

    @staticmethod
    def get_cell_state_in_row(row: Tag, field_name: str) -> tuple:
        """``(has_value, container_count)`` for a cell in a row."""
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
        """Remove every item-container in a cell and leave one empty one.

        Returns the new item id ('' when the cell does not exist).
        """
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
    def next_item_id_in_cell(row: Tag, field_name: str) -> Optional[str]:
        """The id the next appended item in this cell would get (max+1).

        Deterministic from the row's current state, so the id computed at
        event-creation time and the id recomputed during replay always agree.
        """
        cell = HTMLParser._get_cell_in_row(row, field_name)
        if not cell:
            return None
        containers = cell.find_all('span', class_='item-container', recursive=False)
        indices = []
        for c in containers:
            cparts = (c.get('id') or '').split('-')
            if len(cparts) >= 3 and cparts[-1].isdigit():
                indices.append(int(cparts[-1]))
        new_index = (max(indices) + 1) if indices else 0
        row_part = (row.get('id') or 'row0')[3:]
        return f"{row_part}-{field_name}-{new_index}"

    @staticmethod
    def add_item_in_cell(soup, row: Tag, field_name: str, value: str = '') -> str:
        """Append a new item-container to a cell (sep added to the previous
        last container).  The new index is ``max(numeric indices) + 1`` so
        ids stay unique after middle-item deletions."""
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
    def apply_tracking_to_row_html(row_html: str, edited_item_ids: List[str],
                                   added_item_ids: List[str], added_row: bool) -> str:
        """Apply edited/added tracking classes to a single row string.

        Mini-parses the row (~1 ms), adds 'edited'/'added' classes to the
        ``.item-data`` spans of tracked items (and 'added' to the ``<tr>``
        for added rows), and re-serialises.
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
    def create_ghost_item_container(soup, item_id: str, value: str,
                                    is_multi_value: bool = False) -> Tag:
        """Create a ghost item-container (red strikethrough deleted value)."""
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
