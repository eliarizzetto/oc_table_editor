"""Upload and validation routes."""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import sys
from pathlib import Path

from services import ValidatorService, SessionManager
from models import Session
from config import MAX_UPLOAD_SIZE, DEFAULT_VERIFY_ID_EXISTENCE

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

# Import oc_validator interface for HTML generation and merging
from oc_validator.interface.gui import make_gui, merge_html_files

router = APIRouter()


def _generate_html(csv_fp: str, report_fp: str, out_fp: str, errors: list) -> None:
    """
    Generate an HTML visualisation for a validated CSV table.

    Wraps ``make_gui`` but safely handles the zero-errors case:  when the
    validation report is empty, ``make_gui`` crashes because it tries to open
    ``'valid_page.html'`` via a bare relative path that does not exist in the
    oc_table_editor working directory.  We detect this case and delegate to
    ``ValidatorService._make_no_errors_html`` instead.

    Args:
        csv_fp:    Path to the original CSV file.
        report_fp: Path to the JSON validation report.
        out_fp:    Destination HTML file path.
        errors:    The list of errors returned by the validator (used to decide
                   which code path to take).
    """
    if not errors:
        ValidatorService._make_no_errors_html(out_fp, csv_fp)
    else:
        make_gui(csv_fp, report_fp, out_fp)


@router.post("/")
async def upload_files(
    metadata_file: Optional[UploadFile] = File(None),
    citations_file: Optional[UploadFile] = File(None),
    verify_id_existence: bool = Form(DEFAULT_VERIFY_ID_EXISTENCE)
):
    """
    Upload CSV files and run initial validation.

    - **metadata_file**: Optional metadata CSV file
    - **citations_file**: Optional citations CSV file
    - **verify_id_existence**: Whether to check ID existence (external APIs)
    """
    # ── file-size checks ──────────────────────────────────────────────────────
    has_metadata = False
    has_citations = False

    if metadata_file:
        metadata_file.file.seek(0, 2)
        size = metadata_file.file.tell()
        metadata_file.file.seek(0)
        if size > 0 and size <= MAX_UPLOAD_SIZE:
            has_metadata = True
        elif size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Metadata file exceeds maximum size of {MAX_UPLOAD_SIZE / (1024*1024)} MB"
            )

    if citations_file:
        citations_file.file.seek(0, 2)
        size = citations_file.file.tell()
        citations_file.file.seek(0)
        if size > 0 and size <= MAX_UPLOAD_SIZE:
            has_citations = True
        elif size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Citations file exceeds maximum size of {MAX_UPLOAD_SIZE / (1024*1024)} MB"
            )

    if not has_metadata and not has_citations:
        raise HTTPException(status_code=400, detail="At least one CSV file must be provided")

    # ── session creation ──────────────────────────────────────────────────────
    session_id = SessionManager.create_session_id()
    session_dir = SessionManager.create_session_dir(session_id)

    session = Session(
        session_id=session_id,
        has_metadata=has_metadata,
        has_citations=has_citations,
        verify_id_existence=verify_id_existence
    )

    # ── save uploaded files ───────────────────────────────────────────────────
    if has_metadata:
        filename = metadata_file.filename or 'metadata.csv'
        meta_content = await metadata_file.read()
        if not meta_content:
            raise HTTPException(status_code=400, detail="Metadata file is empty.")
        meta_path = await SessionManager.save_uploaded_file(session_id, meta_content, filename)
        session.meta_csv_path = meta_path

    if has_citations:
        filename = citations_file.filename or 'citations.csv'
        cits_content = await citations_file.read()
        if not cits_content:
            raise HTTPException(status_code=400, detail="Citations file is empty.")
        cits_path = await SessionManager.save_uploaded_file(session_id, cits_content, filename)
        session.cits_csv_path = cits_path

    # ── validation + HTML generation ──────────────────────────────────────────
    try:
        if has_metadata and has_citations:
            # ── Paired validation via ClosureValidator ──────────────────────
            meta_errors, cits_errors, meta_report_path, cits_report_path = \
                ValidatorService.validate_pair(
                    meta_csv_path=session.meta_csv_path,
                    cits_csv_path=session.cits_csv_path,
                    meta_output_dir=str(session_dir),
                    cits_output_dir=str(session_dir),
                    verify_id_existence=verify_id_existence
                )

            session.meta_report_path = meta_report_path
            session.cits_report_path = cits_report_path

            # Generate individual HTML tables (saved to meta_table.html /
            # cits_table.html via table_type 'meta' / 'cits').
            meta_table_path = session_dir / 'meta_table.html'
            cits_table_path = session_dir / 'cits_table.html'

            _generate_html(session.meta_csv_path, meta_report_path,
                            str(meta_table_path), meta_errors)
            _generate_html(session.cits_csv_path, cits_report_path,
                            str(cits_table_path), cits_errors)

            # Save individual tables through session manager (meta_table.html,
            # cits_table.html) so that re-validation can parse them later.
            with open(meta_table_path, 'r', encoding='utf-8') as f:
                meta_html_content = f.read()
            with open(cits_table_path, 'r', encoding='utf-8') as f:
                cits_html_content = f.read()

            await SessionManager.save_html(session_id, meta_html_content, 'meta')
            await SessionManager.save_html(session_id, cits_html_content, 'cits')

            # Merge the two individual HTMLs into a single display file
            # (meta_html.html, table_type='display').
            merged_path = session_dir / 'meta_html.html'
            merge_html_files(str(meta_table_path), str(cits_table_path),
                             str(merged_path))
            with open(merged_path, 'r', encoding='utf-8') as f:
                merged_content = f.read()
            await SessionManager.save_html(session_id, merged_content, 'display')

            session.meta_html_path = str(meta_table_path)
            session.cits_html_path = str(cits_table_path)

            # Save baseline snapshots for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, meta_html_content, 'meta')
            await SessionManager.save_baseline_snapshot(session_id, cits_html_content, 'cits')

        elif has_metadata:
            # ── Single metadata table ───────────────────────────────────────
            meta_errors, meta_report_path = ValidatorService.validate_metadata(
                csv_path=session.meta_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )

            session.meta_report_path = meta_report_path

            meta_table_path = session_dir / 'meta_table.html'
            _generate_html(session.meta_csv_path, meta_report_path,
                            str(meta_table_path), meta_errors)

            with open(meta_table_path, 'r', encoding='utf-8') as f:
                meta_html_content = f.read()
            await SessionManager.save_html(session_id, meta_html_content, 'meta')

            session.meta_html_path = str(meta_table_path)

            # Save baseline snapshot for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, meta_html_content, 'meta')

        elif has_citations:
            # ── Single citations table ──────────────────────────────────────
            cits_errors, cits_report_path = ValidatorService.validate_citations(
                csv_path=session.cits_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )

            session.cits_report_path = cits_report_path

            cits_table_path = session_dir / 'cits_table.html'
            _generate_html(session.cits_csv_path, cits_report_path,
                            str(cits_table_path), cits_errors)

            with open(cits_table_path, 'r', encoding='utf-8') as f:
                cits_html_content = f.read()
            await SessionManager.save_html(session_id, cits_html_content, 'cits')

            session.cits_html_path = str(cits_table_path)

            # Save baseline snapshot for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, cits_html_content, 'cits')

        # Mark as validated and persist session
        session.mark_validated()
        await SessionManager.save_session(session)

        return {
            "session_id": session_id,
            "has_metadata": session.has_metadata,
            "has_citations": session.has_citations,
            "verify_id_existence": verify_id_existence,
            "html_url": f"/editor/{session_id}"
        }

    except ValueError as e:
        SessionManager.delete_session(session_id)
        import traceback
        traceback.print_exc()
        error_msg = str(e)
        if "delimiter" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="Invalid CSV file format. Please ensure the file is a valid CSV with proper delimiters."
            )
        else:
            raise HTTPException(status_code=400, detail=f"CSV validation error: {error_msg}")
    except HTTPException:
        raise
    except Exception as e:
        SessionManager.delete_session(session_id)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")
