# OC Table Editor

A local web application for validating and editing [OpenCitations](https://opencitations.net)
bibliographic CSV tables. Upload a **metadata** table, a **citations** table, or both
together; the app validates them with
[oc_validator](https://github.com/opencitations/oc_validator), renders the data as an
editable table, and lets you fix the reported issues in place, then re-validate
and export a corrected CSV.

```text
upload CSV  →  validate  →  edit in place  →  re-validate  →  export CSV
```


## Features

- **CSV upload** — single metadata or citations table, or paired tables (cross-table
  checks for entities)
- **Validation pipeline** — wellformedness, syntax, existence, semantics, duplicates,
  cross-table representation; errors and warnings rendered per item
- **Interactive editor** — every table of a paired session is independently editable,
  searchable, filterable and paginated (25 rows/page)
- **Item-level editing** — click any value to edit it; hover a cell for add/clear
  actions; add or delete rows; multi-value fields keep their proper separators
- **Three-state change tracking** — light blue = edited, green = added, red strikethrough
   = deleted (original values always visible)
- **Filters & search** — per-table full-text search, "Show all rows"/"Show Changes
  Only", single-issue view (click a colored square) and issue-type facet filters
- **Session-wide undo/redo** — one stack across both tables, `Ctrl+Z` / `Ctrl+Y`
- **Re-validation** — re-run the validator on your edited data at any time
- **CSV export** — one button per table in paired sessions; original delimiter preserved
- **Draft sessions** — uploads persist automatically; name, reopen, rename or delete
  them from the home page (most recently updated first)
- **Reduced/expanded cells** — cells render compact by default; expand per table or per
  row
- In-app help via the **?** button in the navbar

## Requirements

- **Python 3.13** (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
git clone <repository-url> && cd oc_table_editor
uv sync
```

This creates the `.venv/` virtual environment with all dependencies (locked in
`uv.lock`; dev/test dependencies included).

## Running

```bash
uv run python main.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.
`GET /health` is available for liveness checks.

> The app is designed as a **local, single-user tool**: sessions are guarded by
> in-memory per-session locks, so run a single process/worker (don't put it behind a
> multi-worker deployment). <!-- > Paths and instructions are Windows-first. -->

## Using the app

### Home page

1. **Upload**: pick a metadata CSV and/or a citations CSV. At least one is required;
   uploading both runs the paired validation with cross-table closure checks.
2. **Session name** *(optional)*: shown in the drafts list; unnamed sessions are listed
   by their CSV filename. You can rename later.
3. **Verify ID existence** *(checkbox, off by default)*: asks external APIs whether
   referenced IDs actually exist. Slower (network calls), so it is opt-in.
4. **Saved drafts**: every upload persists as a session. The list shows drafts most
   recently updated first, with reopen/rename/delete actions.

### Editor page

Each table is preceded by a stats header —
`Issues: N (errors: X; warnings: Y) | Invalid rows: Z | Total rows: N` (plus a line stating the document
 is valid when the table has no issues). A fully valid table stays editable, so you can keep
working with it.

- **Edit a value**: click any item to edit or delete it in the modal that opens 
- **Cell actions**: hover a cell to reveal *add item* and *clear cell* buttons; hover
  the row number to delete the row. `+ row` at the bottom of a table appends a new row.
- **Change tracking**: light blue background = edited, green = added, red strikethrough
  = deleted.
- **Validation feedback**: red underline = error, orange underline = warning. Colored
  squares identify error instances (same color = same error): hover for the
  description, click to filter the table down to only the rows containing data involved in that error.
- **Search & filters**: each table has its own search box, an "Issue filters" dropdown to filter by issue type, and the
  *Show all rows* / *Show Changes Only* toggles. *By default the view shows only rows
  with issues or changes*.
- **Undo/redo**: `Ctrl+Z` / `Ctrl+Y` or the sidebar buttons — one shared stack across
  both tables of a paired session.

### Saving, re-validating, exporting

- **💾 Save Changes**: materializes the current state of every table on disk and clears
  the "unsaved changes" badge. Drafts auto-persist either way — saving is a checkpoint,
  not a requirement.
- **Revalidate**: replays your edited rows through the validator and regenerates the
  tables. Note that row numbering restarts from scratch and **the undo history is
  reset** — revalidation starts a new "generation" of the table. The optional
  *verify ID existence* checkbox can be overridden per revalidation run.
- **Export**: downloads the current state as CSV — one button per table in paired
  sessions, with the original delimiter preserved.

## How it works

Editing is **event-sourced**. Each validation run (upload or revalidate) produces an
immutable HTML *baseline* plus a parsed index of it (row ids, per-row character
offsets, values, issue index). Every edit is
appended to a per-table change journal; what you see on screen, what gets exported and
what gets re-validated is always the *baseline + journal events replayed* — computed
with per-row string splices, never by re-parsing the full document. This keeps page
renders fast even on tables of several thousand rows.

```text
upload / revalidate ─▶ validator ─▶ immutable baseline + parsed index
edit / undo / redo   ─▶ events appended to the per-table journal
view / export        ─▶ baseline + journal replay (per-row splices, no full parse)
```

Session state lives under `temp/` (runtime data, gitignored).

## Project structure

```text
oc_table_editor/
├── main.py                 # FastAPI app, page routes, /health
├── config.py               # Configuration constants
├── models/session.py       # Session data model
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
├── templates/              # base / index / editor / error (Jinja2)
├── static/                 # editor.css, editor.js
└── test/                   # pytest suite (unit/ + functional/, shared fixtures in support.py)
```

### HTTP API (all under `/api`)

| Prefix | File | Purpose |
| --- | --- | --- |
| `/api/upload` | `routes/upload.py` | `POST /` — accept CSVs, validate, create session |
| `/api/edit` | `routes/edit.py` | item/row/cell operations, views, undo/redo, commit, revalidate |
| `/api/export` | `routes/export.py` | `POST /` — download edited CSV |
| `/api/draft` | `routes/draft.py` | list/rename/delete sessions |

Page routes live in `main.py`: `/` (upload), `/editor/{session_id}`, `/health`.

## License

Released under the [ISC License](LICENSE).