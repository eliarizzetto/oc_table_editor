"""Unit tests for the row-level DOM surgery helpers (services/html_parser.py).

The row HTML below mirrors the contract of ``oc_validator``'s ``make_gui``
output: ``<td class="field-value {field}">`` cells holding
``<span class="item-container" id="{row}-{field}-{idx}">`` items with
``.item-data`` values, optional ``.issue-icon`` markers, and ``.sep``
separators between multi-value items.
"""
import pytest
from bs4 import BeautifulSoup

from services.html_parser import HTMLParser

ROW_HTML = (
    '<tr id="row7" class="data-row valid-row">'
    '<td class="row-number">7</td>'
    '<td class="field-value id">'
    '<span id="7-id-0" class="item-container">'
    '<span class="item-data">doi:10.1/one</span>'
    '<span class="sep"> </span></span>'
    '<span id="7-id-1" class="item-container">'
    '<span class="item-data">doi:10.1/two</span></span>'
    '</td>'
    '<td class="field-value title">'
    '<span id="7-title-0" class="item-container">'
    '<span class="item-data">Some Title</span></span>'
    '</td>'
    '<td class="field-value author">'
    '<span id="7-author-0" class="item-container">'
    '<span class="item-data">Smith, J.</span>'
    '<span id="e-1" class="issue-icon error author_name_format"></span>'
    '</span></td>'
    '<td class="field-value volume">'
    '<span id="7-volume-empty" class="item-container">'
    '<span class="item-data"></span></span>'
    '</td>'
    '</tr>'
)


def parsed_row():
    bs = BeautifulSoup(ROW_HTML, "html.parser")
    return bs, bs.find("tr")


class TestCellLookup:
    def test_get_cell_in_row_finds_field(self):
        _, row = parsed_row()
        cell = HTMLParser.get_cell_in_row(row, "title")
        assert cell is not None
        assert "field-value" in cell.get("class", [])

    def test_get_cell_in_row_missing_field(self):
        _, row = parsed_row()
        assert HTMLParser.get_cell_in_row(row, "venue") is None

    def test_item_separators_mapping(self):
        assert HTMLParser.ITEM_SEPARATORS["id"] == " "
        assert HTMLParser.ITEM_SEPARATORS["author"] == "; "
        assert HTMLParser.ITEM_SEPARATORS["cited_id"] == " "


class TestItemValues:
    def test_get_item_value(self):
        bs, _ = parsed_row()
        assert HTMLParser.get_item_value_from_soup(bs, "7-id-0") == "doi:10.1/one"

    def test_get_missing_item_value_is_none(self):
        bs, _ = parsed_row()
        assert HTMLParser.get_item_value_from_soup(bs, "7-id-9") is None

    def test_update_item_value(self):
        bs, _ = parsed_row()
        HTMLParser.update_item_value_in_soup(bs, "7-title-0", "New Title")
        assert HTMLParser.get_item_value_from_soup(bs, "7-title-0") == "New Title"

    def test_update_missing_item_raises(self):
        bs, _ = parsed_row()
        with pytest.raises(ValueError, match="not found"):
            HTMLParser.update_item_value_in_soup(bs, "7-title-9", "x")

    def test_remove_item_removes_container_with_its_icon(self):
        bs, row = parsed_row()
        HTMLParser.remove_item_in_soup(bs, "7-author-0")
        assert bs.find("span", id="7-author-0") is None
        # the issue icon inside the removed container goes with it
        assert bs.find("span", id="e-1") is None
        assert HTMLParser.get_cell_in_row(row, "author") is not None

    def test_remove_missing_item_is_a_noop(self):
        bs, _ = parsed_row()
        HTMLParser.remove_item_in_soup(bs, "7-id-9")
        assert bs.find("span", id="7-id-0") is not None


class TestCellState:
    def test_cell_with_values(self):
        _, row = parsed_row()
        assert HTMLParser.get_cell_state_in_row(row, "id") == (True, 2)

    def test_empty_cell_reports_no_value(self):
        _, row = parsed_row()
        assert HTMLParser.get_cell_state_in_row(row, "volume") == (False, 1)

    def test_missing_cell(self):
        _, row = parsed_row()
        assert HTMLParser.get_cell_state_in_row(row, "venue") == (False, 0)


class TestClearCell:
    def test_clear_cell_leaves_one_empty_container(self):
        bs, row = parsed_row()
        new_id = HTMLParser.clear_cell_in_soup(bs, row, "id")
        assert new_id == "7-id-0"
        cell = HTMLParser.get_cell_in_row(row, "id")
        containers = cell.find_all("span", class_="item-container",
                                   recursive=False)
        assert len(containers) == 1
        assert containers[0].get("id") == "7-id-0"
        assert HTMLParser.get_item_value_from_soup(bs, "7-id-0") == ""

    def test_clear_cell_removes_issue_icons(self):
        bs, row = parsed_row()
        HTMLParser.clear_cell_in_soup(bs, row, "author")
        assert bs.find("span", id="e-1") is None

    def test_clear_missing_cell_returns_empty_id(self):
        bs, row = parsed_row()
        assert HTMLParser.clear_cell_in_soup(bs, row, "venue") == ""


class TestItemIds:
    def test_next_item_id_is_max_plus_one(self):
        _, row = parsed_row()
        assert HTMLParser.next_item_id_in_cell(row, "id") == "7-id-2"

    def test_next_item_id_in_cell_with_empty_marker(self):
        """'-empty' ids are skipped by numbering: the next item is index 0."""
        _, row = parsed_row()
        assert HTMLParser.next_item_id_in_cell(row, "volume") == "7-volume-0"

    def test_next_item_id_missing_cell_is_none(self):
        _, row = parsed_row()
        assert HTMLParser.next_item_id_in_cell(row, "venue") is None

    def test_add_item_assigns_max_plus_one_id(self):
        bs, row = parsed_row()
        new_id = HTMLParser.add_item_in_cell(bs, row, "id", "doi:10.1/three")
        assert new_id == "7-id-2"
        assert HTMLParser.get_item_value_from_soup(bs, "7-id-2") == "doi:10.1/three"

    def test_add_item_separates_previous_last_item(self):
        bs, row = parsed_row()
        HTMLParser.add_item_in_cell(bs, row, "id", "doi:10.1/three")
        first = bs.find("span", id="7-id-0")
        sep = first.find("span", class_="sep")
        assert sep is not None and sep.get_text() == " "

    def test_add_item_to_first_in_cell_gets_empty_sep(self):
        bs, row = parsed_row()
        HTMLParser.add_item_in_cell(bs, row, "volume", "12")
        cell = HTMLParser.get_cell_in_row(row, "volume")
        containers = cell.find_all("span", class_="item-container",
                                   recursive=False)
        assert len(containers) == 2
        # 'volume' is single-value: a sep span is still added to the previous
        # last container, but with empty content (nothing visible).
        sep = containers[0].find("span", class_="sep")
        assert sep is not None and sep.get_text() == ""

    def test_add_item_missing_field_raises(self):
        bs, row = parsed_row()
        with pytest.raises(ValueError, match="not found"):
            HTMLParser.add_item_in_cell(bs, row, "venue", "x")


class TestTrackingClasses:
    def test_edited_and_added_classes_applied(self):
        html = HTMLParser.apply_tracking_to_row_html(
            ROW_HTML, edited_item_ids=["7-title-0"],
            added_item_ids=["7-id-1"], added_row=False)
        bs = BeautifulSoup(html, "html.parser")
        assert "edited" in bs.find("span", id="7-title-0").find(
            "span", class_="item-data").get("class", [])
        assert "added" in bs.find("span", id="7-id-1").find(
            "span", class_="item-data").get("class", [])
        assert "added" not in bs.find("tr").get("class", [])

    def test_added_row_marks_the_tr(self):
        html = HTMLParser.apply_tracking_to_row_html(
            ROW_HTML, edited_item_ids=[], added_item_ids=[], added_row=True)
        assert "added" in BeautifulSoup(html, "html.parser").find(
            "tr").get("class", [])

    def test_untouched_items_stay_untracked(self):
        html = HTMLParser.apply_tracking_to_row_html(
            ROW_HTML, edited_item_ids=["7-title-0"], added_item_ids=[],
            added_row=False)
        bs = BeautifulSoup(html, "html.parser")
        data = bs.find("span", id="7-id-0").find("span", class_="item-data")
        assert data.get("class") == ["item-data"]


class TestGhostContainer:
    def test_ghost_item_shape(self):
        bs, _ = parsed_row()
        ghost = HTMLParser.create_ghost_item_container(bs, "7-id-1",
                                                       "doi:10.1/two")
        assert ghost.get("id") == "ghost-7-id-1"
        assert ghost.get("data-ghost-item-id") == "7-id-1"
        assert "deleted-ghost" in ghost.get("class", [])
        data = ghost.find("span", class_="item-data")
        assert data.get_text() == "doi:10.1/two"
        assert "color: #842029" in data.get("style", "")
