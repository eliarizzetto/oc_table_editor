# OC Table Editor

A web application for editing validated CSV tables (metadata and citations) with graphical validation feedback.

## Features

- **CSV Upload**: Support for single metadata or citations tables, or paired tables
- **Validation Pipeline**: Full validation using oc_validator (wellformedness, syntax, existence, semantics, duplicates, closure)
- **HTML Editor**: Interactive editing of validated data with validation feedback
- **Individual Span Editing**: Edit each data element or separator independently
- **Three-State Change Tracking**: 
  - **Edited items**: Grey background (modified existing data)
  - **Added items/rows**: Green background (newly created data)
  - **Deleted data**: Red strikethrough overlay (removed data - coming soon)
- **Change Filters**: 
  - "Show All": Display complete table
  - "Show Changes": View only edited and added data
  - "Show Deleted": View deleted data (coming soon)
- **Row and Item Operations**: Add/delete rows, add/delete items, clear cells
- **Undo/Redo**: Full undo/redo support with keyboard shortcuts (Ctrl+Z, Ctrl+Y)
- **CSV Export**: Export edited data back to original CSV format
- **Draft Management**: Save and restore editing sessions
- **Local Browser**: Runs entirely in browser (static file mode)

## Project Structure

```
oc_table_editor/
├── main.py                 # FastAPI application entry point
├── config.py               # Configuration settings
├── requirements.txt        # Python dependencies
├── models/
│   └── session.py         # Session data model
├── services/
│   ├── validator_service.py   # oc_validator integration
│   ├── session_manager.py     # Session management
│   ├── html_parser.py         # HTML parsing for edit/extract
│   └── csv_exporter.py        # CSV export functionality
├── routes/
│   ├── upload.py          # File upload endpoints
│   ├── edit.py            # Edit operation endpoints
│   ├── export.py          # CSV export endpoint
│   └── draft.py           # Draft management endpoints
├── templates/
│   ├── base.html          # Base template
│   ├── index.html         # Upload page
│   ├── editor.html        # Editor page
│   └── error.html         # Error page
└── static/
    ├── css/
    │   └── editor.css     # Editor styles
    └── js/
        └── editor.js      # Editor JavaScript logic
```

## Installation

1. Create a virtual environment:
```bash
cd C:\Users\media\Lavoro\oc_table_editor
python -m venv venv
venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Running the Application

### Server Mode (Full Features)

```bash
python main.py --mode server
```

Then open `http://localhost:8000` in your browser.

### Static File Mode (Browser-Only)

```bash
python main.py --mode static
```

This generates HTML files that can be opened directly in a browser without a running server (no session management or persistent storage).

## Usage

1. **Upload**: Upload CSV files (metadata or citations, single or paired)
2. **Validate**: Click "Re-Validate" to run the validation pipeline
3. **Edit**: Click on any data element to edit:
   - Click on text to edit the content
   - Hover over cells to see action buttons (add, clear, delete)
   - Use "+ add" to append values to multi-value fields
   - Use "+ Add new row" to add new empty rows
4. **Track Changes**:
   - **Grey background**: Edited items (modified existing data)
   - **Green background**: Added items or rows (newly created data)
   - Use filters to view specific changes:
     - "Show All": Display complete table
     - "Show Changes": View only edited and added data
     - "Show Deleted": View deleted data (coming soon)
5. **Undo/Redo**: Use Ctrl+Z (undo) and Ctrl+Y (redo) or the sidebar buttons
6. **Export**: Click "Export CSV" to download the edited data in CSV format
7. **Save Draft**: Click "Save Draft" to save your editing session

## Validation Feedback

- **Red underline**: Error
- **Orange underline**: Warning
- **Colored squares**: Represent error instances (same color = same error)
- **Click on square**: Highlights all data involved in that error
- **Hover on square**: Shows error description

## Technical Details

- **Backend**: FastAPI (Python)
- **Frontend**: Bootstrap 5 + custom JavaScript
- **Validation**: oc_validator library
- **HTML Parsing**: BeautifulSoup for edit tracking and data extraction
- **File Format**: Generates OC validator-compatible HTML reports

## License

This project uses the oc_validator library, which is licensed under a permissive license. See the oc_validator repository for details.