# OC Table Editor

A FastAPI web application for editing validated bibliographic CSV tables (metadata and citations).

## Features

- **CSV Upload**: Single metadata or citations tables, or paired tables (cross-table closure checks)
- **Validation Pipeline**: Full validation using oc_validator (wellformedness, syntax, existence, semantics, duplicates, closure)
- **HTML Editor**: Interactive, paginated editing of validated data — every table of a paired session is independently editable, filterable and searchable
- **Individual Span Editing**: Edit each data element independently
- **Three-State Change Tracking**:
  - **Edited items**: Grey background (modified existing data)
  - **Added items/rows**: Green background (newly created data)
  - **Deleted data**: Red strikethrough ghost overlays with the original values, always visible
- **Change Filters**: "Show all rows" / "Show Changes Only" per table, full-table search, single-issue view (click a colored square) and issue-type facet filters
- **Row and Item Operations**: Add/delete rows, add/delete items, clear cells
- **Undo/Redo**: Session-wide undo stack across both tables, with keyboard shortcuts (Ctrl+Z, Ctrl+Y)
- **CSV Export**: Export each table back to CSV (original delimiter preserved)
- **Session Management**: Uploads persist automatically as sessions; name them, list them (most recently updated first), reopen or delete them from the home page
- **Reduced/Expanded Cells**: Cells render reduced by default (one line per valid cell, invalid items always in full); expand per table or per row

## Project Structure

```text
oc_table_editor/
├── main.py                 # FastAPI application entry point
├── config.py               # Configuration settings
├── pyproject.toml          # Python dependencies (uv-managed, locked in uv.lock)
├── models/
│   └── session.py          # Session data model
├── services/
│   ├── journal.py          # Append-only change journals (source of truth while editing)
│   ├── view_builder.py     # Generation artifacts + journal replay (table views)
│   ├── session_document.py # Per-session locks, atomic writes, file-name scheme
│   ├── session_manager.py  # Session identity, uploaded files, artifact persistence
│   ├── table_scope.py      # Shared meta/cits table-type resolution
│   ├── validator_service.py# oc_validator integration + HTML generation
│   ├── html_parser.py      # Row-level DOM surgery (contracted with make_gui output)
│   └── csv_exporter.py     # CSV export
├── routes/
│   ├── upload.py           # File upload endpoint
│   ├── edit.py             # Edit/view/undo/commit/revalidate endpoints
│   ├── export.py           # CSV export endpoint
│   └── draft.py            # Session list/rename/delete endpoints
├── templates/
│   ├── base.html           # Base template
│   ├── index.html          # Upload page
│   ├── editor.html          # Editor page
│   └── error.html          # Error page
└── static/
    ├── css/
    │   └── editor.css      # Editor styles
    └── js/
        └── editor.js       # Editor JavaScript logic
```

## Installation

The project is managed with [uv](https://docs.astral.sh/uv/) (dependencies in `pyproject.toml`, locked in `uv.lock`, Python version in `.python-version`):

```bash
cd C:\Users\media\Lavoro\oc_table_editor
uv sync
```

This creates/updates the `.venv` virtual environment with all dependencies.

## Running the Application

```bash
uv run python main.py
```

(Equivalently: `.venv\Scripts\activate` then `python main.py`.)

Then open `http://localhost:8000` in your browser.

## Usage

1. **Upload**: Upload CSV files (metadata or citations, single or paired), optionally naming the session
2. **Edit**: Click on any data element to edit it:
   - Click on text to edit the content
   - Hover over cells to see action buttons (add, clear, delete)
   - Use "+" to append values to multi-value fields
   - Use "+ row" at the bottom of a table to add new rows
3. **Track Changes**:
   - **Grey background**: Edited items
   - **Green background**: Added items or rows
   - **Red strikethrough ghosts**: Deleted data (always visible)
   - Filter with "Show all rows" / "Show Changes Only", the search box, or the issue filters
4. **Undo/Redo**: Ctrl+Z / Ctrl+Y or the sidebar buttons (works across both tables)
5. **Save Changes**: "💾 Save Changes" materializes the current state into the table files (drafts auto-persist; this clears the unsaved badge)
6. **Re-Validate**: Re-runs validation on the edited data and regenerates the tables
7. **Export**: Download the edited data as CSV (one button per table in paired sessions)

## Validation Feedback

- **Red underline**: Error
- **Orange underline**: Warning
- **Colored squares**: Represent error instances (same color = same error)
- **Click on square**: Shows only the data involved in that error
- **Hover on square**: Shows error description

## Technical Details

- **Backend**: FastAPI (Python), single process/worker (in-memory per-session locks)
- **Frontend**: Bootstrap 5 + custom JavaScript
- **Validation**: oc_validator library
- **Editing model**: Event-sourced — the validated table is an immutable baseline; every edit is a journal event, and views (page renders, export, revalidate) are computed by replaying events. BeautifulSoup is used only for artifact building and per-row mini-parses.
- **File Format**: Generates OC validator-compatible HTML tables

## License

This project uses the oc_validator library, which is licensed under a permissive license. See the oc_validator repository for details.
