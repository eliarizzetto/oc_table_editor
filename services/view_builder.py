"""Generation artifacts + journal replay — computes table views without parsing.

The baseline HTML (``baseline_*.html``) is immutable per validation
*generation*.  ``build_generation_artifacts`` parses it **once** per
generation (upload / revalidate, inside those already-slow requests) and
writes ``artifacts_{table_type}.json``:

    {generation, has_table, headers, row_ids (ordered),
     row_offsets {row_id: [start, end]} (char offsets into the baseline),
     table_open_tag, thead_html,
     rows {row_id: {field: [[item_id, value], ...]}},
     issue_index {issue_id: [row_ids]}}

``TableView`` then replays journal events against that base using per-row
mini-parses (~1 ms each) and a single-pass string assembly — no full-document
parse, no full serialize, no 12 MB writes at runtime.
"""
import asyncio
import json
import re
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiofiles import open as aio_open
from bs4 import BeautifulSoup

from config import TEMP_DIR
from services.html_parser import HTMLParser
from services.session_document import (
    SpliceError,
    atomic_write,
)

# Matches <tr ... id="rowN" ...> regardless of attribute order
_ROW_TAG_RE = re.compile(r'<tr[^>]*\sid="row(\d+)"')

# In-memory cache of (artifacts + base string) per session table
_STATE_CACHE: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
_STATE_CACHE_MAX = 8


def _artifacts_path(session_id: str, table_type: str) -> Path:
    return TEMP_DIR / session_id / f'artifacts_{table_type}.json'


def _baseline_path(session_id: str, table_type: str) -> Path:
    return TEMP_DIR / session_id / f'baseline_{table_type}.html'


# ---------------------------------------------------------------------------
# Artifact building (once per generation)
# ---------------------------------------------------------------------------

def _build_artifacts_sync(base_html: str) -> dict:
    """Full parse of the baseline → artifact dict (no generation id yet)."""
    soup = BeautifulSoup(base_html, 'html.parser')
    table = soup.find('table', id='table-data')
    if table is None:
        return {'has_table': False, 'headers': [], 'row_ids': [],
                'row_offsets': {}, 'table_open_tag': '', 'thead_html': '',
                'rows': {}, 'issue_index': {}}

    thead = table.find('thead')
    header_row = thead.find('tr') if thead else None
    headers = ([th.get_text(strip=True) for th in header_row.find_all('th')][1:]
               if header_row else [])

    table_class = table.get('class', [])
    class_attr = ' '.join(table_class) if isinstance(table_class, list) else table_class
    table_open_tag = f'<table class="{class_attr}" id="table-data">'
    thead_html = str(thead) if thead else ''

    # Row structure from the parse
    rows: Dict[str, dict] = {}
    issue_index: Dict[str, List[str]] = {}
    parse_row_ids: List[str] = []
    tbody = table.find('tbody')
    for tr in (tbody.find_all('tr') if tbody else []):
        rid = tr.get('id')
        if not rid:
            continue
        parse_row_ids.append(rid)
        fields: Dict[str, list] = {}
        cells = tr.find_all('td')[1:]
        for header, cell in zip(headers, cells):
            items: List[list] = []
            for container in cell.find_all('span', class_='item-container',
                                            recursive=False):
                cid = container.get('id', '')
                data = container.find('span', class_='item-data')
                if data is not None:
                    items.append([cid, data.get_text(strip=False)])
            fields[header] = items
            for icon in cell.find_all('span', class_='issue-icon', id=True):
                issue_index.setdefault(icon.get('id'), []).append(rid)
        rows[rid] = fields

    # Row byte offsets via a single regex pass over the raw string
    row_ids: List[str] = []
    row_offsets: Dict[str, Tuple[int, int]] = {}
    for m in _ROW_TAG_RE.finditer(base_html):
        rid = f'row{m.group(1)}'
        end = base_html.find('</tr>', m.end())
        if end == -1:
            break
        row_ids.append(rid)
        row_offsets[rid] = (m.start(), end + len('</tr>'))

    if row_ids != parse_row_ids:
        raise ValueError(
            f'Baseline row scan mismatch (regex {len(row_ids)} vs parse '
            f'{len(parse_row_ids)} rows) — refusing to build artifacts')

    return {'has_table': True, 'headers': headers, 'row_ids': row_ids,
            'row_offsets': {k: list(v) for k, v in row_offsets.items()},
            'table_open_tag': table_open_tag, 'thead_html': thead_html,
            'rows': rows, 'issue_index': issue_index}


async def build_generation_artifacts(session_id: str, table_type: str,
                                     base_html: Optional[str] = None) -> str:
    """Parse the baseline once and persist artifacts; returns the new generation id.

    Runs the parse in a worker thread (9-12 s on a 12 MB table) — call from
    upload/revalidate where the cost is absorbed.
    """
    if base_html is None:
        path = _baseline_path(session_id, table_type)
        async with aio_open(path, 'r', encoding='utf-8', newline='') as f:
            base_html = await f.read()

    artifacts = await asyncio.to_thread(_build_artifacts_sync, base_html)
    artifacts['generation'] = str(uuid.uuid4())
    await atomic_write(_artifacts_path(session_id, table_type),
                       json.dumps(artifacts))
    _STATE_CACHE.pop((session_id, table_type), None)
    return artifacts['generation']


async def load_table_state(session_id: str, table_type: str) -> Optional[dict]:
    """Load (and cache) the base string + artifacts for a session table."""
    key = (session_id, table_type)
    cached = _STATE_CACHE.get(key)
    if cached is not None:
        _STATE_CACHE.move_to_end(key)
        return cached

    base_path = _baseline_path(session_id, table_type)
    if not base_path.exists() or base_path.stat().st_size == 0:
        return None
    async with aio_open(base_path, 'r', encoding='utf-8', newline='') as f:
        base_html = await f.read()

    art_path = _artifacts_path(session_id, table_type)
    if art_path.exists():
        async with aio_open(art_path, 'r', encoding='utf-8') as f:
            artifacts = json.loads(await f.read())
    else:
        # Artifacts missing but baseline present — rebuild (rare; e.g. the
        # baseline file was restored manually). Costs one full parse.
        await build_generation_artifacts(session_id, table_type, base_html)
        async with aio_open(art_path, 'r', encoding='utf-8') as f:
            artifacts = json.loads(await f.read())

    state = {'base_html': base_html, 'artifacts': artifacts}
    _STATE_CACHE[key] = state
    while len(_STATE_CACHE) > _STATE_CACHE_MAX:
        _STATE_CACHE.popitem(last=False)
    return state


def drop_session_states(session_id: str) -> None:
    """Evict cached table states for a session (draft delete)."""
    for key in [k for k in _STATE_CACHE if k[0] == session_id]:
        _STATE_CACHE.pop(key, None)


async def load_journal_view(session_id: str, table_type: str):
    """Load (journal, view, table_state) for a session table.

    Caller must hold the session lock.  Returns None when the baseline
    (base) is missing.  Discards journal events stamped with a generation
    other than the current artifacts' generation.
    """
    from services.journal import ChangeJournal  # local import: journal imports session_document only

    state = await load_table_state(session_id, table_type)
    if state is None:
        return None
    journal = await ChangeJournal.load(
        session_id,
        expected_generation=state['artifacts']['generation'],
        table_type=table_type,
    )
    view = TableView(state['base_html'], state['artifacts'],
                     journal.applied_events)
    return journal, view, state


# ---------------------------------------------------------------------------
# Row synthesis / parsing helpers
# ---------------------------------------------------------------------------

def synthesize_row_html(row_id: str, headers: List[str]) -> str:
    """Build the HTML of a newly added (empty) row — byte-identical in shape
    to what ``HTMLParser.add_row_to_soup`` produces."""
    bs = BeautifulSoup('', 'html.parser')
    row_number = row_id[3:]
    tr = bs.new_tag('tr', attrs={'id': row_id})
    num_cell = bs.new_tag('td', attrs={'class': 'row-number'})
    num_cell.string = row_number
    tr.append(num_cell)
    for field in headers:
        cell = bs.new_tag('td', attrs={'class': ['field-value', field]})
        container = bs.new_tag('span', attrs={
            'class': 'item-container', 'id': f'{row_number}-{field}-0'})
        data = bs.new_tag('span', attrs={
            'class': 'item-data', 'style': 'cursor: pointer;'})
        data.string = ''
        container.append(data)
        cell.append(container)
        tr.append(cell)
    return str(tr)


def parse_row_fields(row_html: str) -> Dict[str, List[Tuple[str, str]]]:
    """Mini-parse a row string → {field: [(item_id, value), ...]}."""
    bs = BeautifulSoup(row_html, 'html.parser')
    row = bs.find('tr')
    if row is None:
        return {}
    result: Dict[str, List[Tuple[str, str]]] = {}
    for td in row.find_all('td', class_='field-value'):
        classes = td.get('class', [])
        field = None
        if 'field-value' in classes:
            idx = classes.index('field-value')
            if idx + 1 < len(classes):
                field = classes[idx + 1]
        if not field:
            continue
        items: List[Tuple[str, str]] = []
        for container in td.find_all('span', class_='item-container',
                                     recursive=False):
            cid = container.get('id', '')
            data = container.find('span', class_='item-data')
            if data is not None:
                items.append((cid, data.get_text(strip=False)))
        result[field] = items
    return result


# ---------------------------------------------------------------------------
# View: base + replayed journal events
# ---------------------------------------------------------------------------

class TableView:
    """The current table state: baseline + journal events up to the cursor.

    Computed synchronously from (base_html, artifacts, events); costs
    O(affected rows × ~1 ms) plus one single-pass string assembly.
    """

    def __init__(self, base_html: str, artifacts: dict, events: list):
        self.base_html = base_html
        self.artifacts = artifacts
        self.events = events

        by_row: "OrderedDict[str, list]" = OrderedDict()
        for ev in events:
            by_row.setdefault(ev['row'], []).append(ev)

        # Row existence is order-aware: a row is currently dead when its
        # LAST add_row/delete_row event is a delete.  ``next_add_row_id``
        # can reuse the id of a deleted added row, so "any delete event
        # exists" would wrongly kill the re-added row.
        last_row_op: Dict[str, str] = {}
        for ev in events:
            if ev['op'] in ('add_row', 'delete_row'):
                last_row_op[ev['row']] = ev['op']
        self.deleted_row_ids = {rid for rid, op in last_row_op.items()
                                if op == 'delete_row'}
        self.added_row_ids = [rid for rid in dict.fromkeys(
                                  ev['row'] for ev in events
                                  if ev['op'] == 'add_row')
                              if last_row_op[rid] == 'add_row']
        base_row_ids: List[str] = artifacts.get('row_ids', [])
        self.row_ids = ([r for r in base_row_ids
                         if r not in self.deleted_row_ids]
                        + self.added_row_ids)

        # Replay per affected row
        self.edited_item_ids: set = set()
        self.added_item_ids: set = set()
        self._row_html_overrides: Dict[str, str] = {}
        for rid, evs in by_row.items():
            if rid in self.deleted_row_ids:
                continue
            edited, added, row_html = self._replay_row(rid, evs)
            self.edited_item_ids |= edited
            self.added_item_ids |= added
            self._row_html_overrides[rid] = row_html

        self._html = self._assemble()

    # -- per-row replay --------------------------------------------------------

    def _base_row_html(self, rid: str) -> str:
        if rid in self.added_row_ids:
            return synthesize_row_html(rid, self.artifacts['headers'])
        s, e = self.artifacts['row_offsets'][rid]
        return self.base_html[s:e]

    def _replay_row(self, rid: str, evs: list) -> Tuple[set, set, str]:
        """Apply one row's events to a mini-parsed base row.

        Returns (edited_item_ids, added_item_ids, row_html_with_classes).
        """
        bs = BeautifulSoup(self._base_row_html(rid), 'html.parser')
        row = bs.find('tr')
        edited: set = set()
        added: set = set()
        for ev in evs:
            op = ev['op']
            if op == 'set_item':
                HTMLParser.update_item_value_in_soup(bs, ev['item'], ev['value'])
                edited.add(ev['item'])
            elif op == 'init_cell':
                new_id = HTMLParser.clear_cell_in_soup(bs, row, ev['field'])
                HTMLParser.update_item_value_in_soup(bs, new_id, ev['value'])
                self._drop_field_states(rid, ev['field'], edited, added)
                added.add(new_id)
            elif op == 'append_item':
                new_id = HTMLParser.add_item_in_cell(bs, row, ev['field'],
                                                     ev['value'])
                added.add(new_id)
            elif op == 'remove_item':
                HTMLParser.remove_item_in_soup(bs, ev['item'])
                edited.discard(ev['item'])
                added.discard(ev['item'])
            elif op == 'clear_cell':
                HTMLParser.clear_cell_in_soup(bs, row, ev['field'])
                self._drop_field_states(rid, ev['field'], edited, added)
            # 'add_row' / 'delete_row' need no row-local replay
        row_html = HTMLParser.apply_tracking_to_row_html(
            str(row), sorted(edited), sorted(added), rid in self.added_row_ids)
        return edited, added, row_html

    def _drop_field_states(self, rid: str, field: str,
                           edited: set, added: set) -> None:
        for item_id, _ in self.artifacts['rows'].get(rid, {}).get(field, []):
            edited.discard(item_id)
            added.discard(item_id)

    # -- assembly ----------------------------------------------------------------

    def _assemble(self) -> str:
        overrides = dict(self._row_html_overrides)
        for rid in self.deleted_row_ids:
            overrides[rid] = None  # deletion marker

        pieces: List[str] = []
        last = 0
        base_row_ids = self.artifacts.get('row_ids', [])
        for rid in base_row_ids:
            if rid not in overrides:
                continue
            s, e = self.artifacts['row_offsets'][rid]
            pieces.append(self.base_html[last:s])
            rep = overrides[rid]
            if rep is not None:
                pieces.append(rep)
            last = e
        pieces.append(self.base_html[last:])
        html_out = ''.join(pieces)

        # Append added rows before </tbody> (single splice)
        if self.added_row_ids:
            added_html = ''.join(self._row_html_overrides.get(
                rid, synthesize_row_html(rid, self.artifacts['headers']))
                for rid in self.added_row_ids)
            idx = html_out.find('</tbody>')
            if idx == -1:
                raise SpliceError('No </tbody> found to append added rows')
            html_out = html_out[:idx] + added_html + html_out[idx:]
        return html_out

    # -- public accessors ----------------------------------------------------------

    @property
    def html(self) -> str:
        return self._html

    def row_html(self, rid: str) -> Optional[str]:
        """Current (tracked) HTML of a view row, or None if not in the view."""
        if rid not in self.row_ids:
            return None
        if rid in self._row_html_overrides:
            return self._row_html_overrides[rid]
        if rid in self.added_row_ids:
            return synthesize_row_html(rid, self.artifacts['headers'])
        s, e = self.artifacts['row_offsets'][rid]
        return self.base_html[s:e]

    def row_soup(self, rid: str):
        """Live mini-soup of a row's current state (no tracking classes) —
        for mutation validation.  Returns (bs, row_tag) or (None, None)."""
        html = self.row_html(rid)
        if html is None:
            return None, None
        if rid in self._row_html_overrides or rid in self.added_row_ids:
            # Strip classes by replaying without tracking
            html = self._untracked_row_html(rid)
        bs = BeautifulSoup(html, 'html.parser')
        return bs, bs.find('tr')

    def _untracked_row_html(self, rid: str) -> str:
        by_row = OrderedDict()
        for ev in self.events:
            if ev['row'] == rid:
                by_row.setdefault(rid, []).append(ev)
        evs = by_row.get(rid, [])
        if not evs:
            return self._base_row_html(rid)
        bs = BeautifulSoup(self._base_row_html(rid), 'html.parser')
        row = bs.find('tr')
        for ev in evs:
            op = ev['op']
            if op == 'set_item':
                HTMLParser.update_item_value_in_soup(bs, ev['item'], ev['value'])
            elif op == 'init_cell':
                new_id = HTMLParser.clear_cell_in_soup(bs, row, ev['field'])
                HTMLParser.update_item_value_in_soup(bs, new_id, ev['value'])
            elif op == 'append_item':
                HTMLParser.add_item_in_cell(bs, row, ev['field'], ev['value'])
            elif op == 'remove_item':
                HTMLParser.remove_item_in_soup(bs, ev['item'])
            elif op == 'clear_cell':
                HTMLParser.clear_cell_in_soup(bs, row, ev['field'])
        return str(row)

    def next_row_id_after(self, rid: str) -> Optional[str]:
        try:
            idx = self.row_ids.index(rid)
        except ValueError:
            return None
        return self.row_ids[idx + 1] if idx + 1 < len(self.row_ids) else None

    def next_add_row_id(self) -> str:
        numbers = [int(r[3:]) for r in self.row_ids if r.startswith('row')
                   and r[3:].isdigit()]
        return f'row{(max(numbers) + 1) if numbers else 0}'

    def rows_for_export(self) -> List[Dict[str, List[str]]]:
        """Rows as {field: [values]} in view order — the same shape
        ``HTMLParser.parse_table`` produced (CSVExporter consumes it)."""
        headers = self.artifacts['headers']
        out: List[Dict[str, List[str]]] = []
        for rid in self.row_ids:
            row_dict: Dict[str, List[str]] = {}
            if rid in self._row_html_overrides or rid in self.added_row_ids:
                fields = parse_row_fields(self.row_html(rid))
                for header in headers:
                    row_dict[header] = [v for _, v in fields.get(header, [])]
            else:
                base_fields = self.artifacts['rows'].get(rid, {})
                for header in headers:
                    row_dict[header] = [v for _, v in base_fields.get(header, [])]
            out.append(row_dict)
        return out

    # -- display rows (filtering + pagination) -----------------------------------

    def display_rows(self) -> List[dict]:
        """Ordered display list: base rows (deleted ones as ghosts) + added rows.

        The single source of row order for filtering/pagination.  Iterating
        ``artifacts['row_ids']`` puts each ghost at its numeric position and
        keeps added rows last; added-then-deleted rows are excluded from
        ``added_row_ids`` entirely, so they correctly leave no ghost.
        """
        out: List[dict] = [{'row_id': rid, 'ghost': rid in self.deleted_row_ids}
                           for rid in self.artifacts.get('row_ids', [])]
        out.extend({'row_id': rid, 'ghost': False}
                   for rid in self.added_row_ids)
        return out

    def changed_row_ids(self, deletions: Optional[dict] = None) -> set:
        """Ids of rows with any edit, addition or deletion (incl. ghost rows)."""
        if deletions is None:
            deletions = self.compute_deletions()
        changed = {'row' + iid.split('-')[0]
                   for iid in (self.edited_item_ids | self.added_item_ids
                               | set(deletions['deleted_items']))}
        changed |= set(deletions['deleted_rows'])
        changed |= set(self.added_row_ids)
        return changed

    def issue_row_ids(self) -> set:
        """Ids of base rows carrying any issue icon (errors or warnings)."""
        out: set = set()
        for row_ids in self.artifacts.get('issue_index', {}).values():
            out |= set(row_ids)
        return out

    def row_html_with_ghosts(self, rid: str, deleted_items: List[str],
                             values: Dict[str, str]) -> str:
        """A view row's HTML with ghost containers for its deleted items.

        ``deleted_items`` must be this row's item ids only — callers group
        ``compute_deletions()['deleted_items']`` by row prefix so each
        affected row is mini-parsed once.
        """
        html = self.row_html(rid) or ''
        if not deleted_items:
            return html
        bs = BeautifulSoup(html, 'html.parser')
        row = bs.find('tr')
        if row is None:
            return html
        for item_id in deleted_items:
            parts = item_id.split('-')
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            field_name = '-'.join(parts[1:-1])
            cell = HTMLParser._get_cell_in_row(row, field_name)
            if not cell:
                continue
            ghost_item = HTMLParser.create_ghost_item_container(
                bs, item_id, values.get(item_id, ''),
                field_name in HTMLParser.ITEM_SEPARATORS)
            item_index = int(parts[-1]) if parts[-1].isdigit() else -1
            inserted = False
            for container in cell.find_all('span', class_='item-container',
                                           recursive=False):
                cparts = container.get('id', '').split('-')
                if len(cparts) >= 3 and cparts[-1].isdigit():
                    if int(cparts[-1]) > item_index:
                        container.insert_before(ghost_item)
                        inserted = True
                        break
            if not inserted:
                cell.append(ghost_item)
        return str(row)

    # -- deletions (ghost view) ------------------------------------------------------

    def compute_deletions(self) -> dict:
        """Per-row base-vs-view diff: deleted items/rows with their values.

        Mirrors the old ``identify_deletions_with_values`` semantics:
        - items present in base but absent from the view are deleted;
        - items present in both but emptied (edit-to-empty) are deleted;
        - added rows/items never ghost (they are not in the base).
        """
        deleted_items: List[str] = []
        deleted_item_values: Dict[str, str] = {}
        rows_with_events = {ev['row'] for ev in self.events}
        for rid in rows_with_events:
            if rid in self.deleted_row_ids or rid in self.added_row_ids:
                continue
            if rid not in self.artifacts['rows']:
                continue
            base_fields = self.artifacts['rows'][rid]
            view_fields = parse_row_fields(self.row_html(rid) or '')
            base_flat = {iid: val for field_items in base_fields.values()
                         for iid, val in field_items}
            view_flat = {iid: val for field_items in view_fields.values()
                         for iid, val in field_items}
            for iid, bval in base_flat.items():
                if iid not in view_flat:
                    deleted_items.append(iid)
                    deleted_item_values[iid] = bval
                elif bval.strip() and not view_flat[iid].strip():
                    deleted_items.append(iid)
                    deleted_item_values[iid] = bval
        return {
            'deleted_items': deleted_items,
            'deleted_rows': sorted((r for r in self.deleted_row_ids
                                    if r in self.artifacts.get('rows', {})),
                                   key=lambda r: int(r[3:]) if r[3:].isdigit() else 0),
            'deleted_item_values': deleted_item_values,
        }

    def ghost_row_html(self, rid: str) -> str:
        """Transform a base row into a ghost row (red strikethrough view)."""
        s, e = self.artifacts['row_offsets'][rid]
        bs = BeautifulSoup(self.base_html[s:e], 'html.parser')
        row = bs.find('tr')
        if row is None:
            return ''
        classes = row.get('class', [])
        if isinstance(classes, list):
            if 'deleted' not in classes:
                classes.append('deleted')
            row['class'] = classes
        row['id'] = f'ghost-{rid}'
        row['data-ghost-row-id'] = rid
        for container in row.find_all('span', class_='item-container'):
            cclasses = container.get('class', [])
            if isinstance(cclasses, list):
                if 'deleted-ghost' not in cclasses:
                    cclasses.append('deleted-ghost')
                container['class'] = cclasses
            data = container.find('span', class_='item-data')
            if data is not None:
                data['style'] = 'color: #842029; font-style: italic;'
        return str(row)
