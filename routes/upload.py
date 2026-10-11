"""Upload and validation routes."""
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional

from services import ValidatorService, SessionManager
from services.journal import ChangeJournal
from services.session_document import document_cache
from services.view_builder import build_generation_artifacts
from models import Session
from config import MAX_UPLOAD_SIZE, DEFAULT_VERIFY_ID_EXISTENCE, MAX_DRAFT_NAME_LEN
from oc_validator.main import TableNotMatchingInstance

router = APIRouter()


def _check_csv_header(filename: str, content: bytes) -> None:
    """
    Reject CSVs whose header contains characters that oc_validator would
    interpolate unescaped into HTML class names / element ids (<, >, ").
    Such headers already produce broken table markup today; failing early
    gives a clear message instead.
    """
    header = content.decode('utf-8-sig', errors='replace').split('\n', 1)[0]
    bad = [ch for ch in '<>"' if ch in header]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}': CSV header contains characters that cannot be "
                   f"rendered in the editor ({', '.join(bad)}). "
                   f"Please fix the header row."
        )


@router.post("/")
async def upload_files(
    metadata_file: Optional[UploadFile] = File(None),
    citations_file: Optional[UploadFile] = File(None),
    verify_id_existence: bool = Form(DEFAULT_VERIFY_ID_EXISTENCE),
    draft_name: Optional[str] = Form(None)
):
    """
    Upload CSV files and run initial validation.

    - **metadata_file**: Optional metadata CSV file
    - **citations_file**: Optional citations CSV file
    - **verify_id_existence**: Whether to check ID existence (external APIs)
    - **draft_name**: Optional user-assigned session name
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

    # Optional user-assigned name (shown in the saved-drafts list; renamable)
    clean_name = (draft_name or '').strip()
    if len(clean_name) > MAX_DRAFT_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Session name must be at most {MAX_DRAFT_NAME_LEN} characters")

    # ── session creation ──────────────────────────────────────────────────────
    session_id = SessionManager.create_session_id()
    session_dir = SessionManager.create_session_dir(session_id)

    session = Session(
        session_id=session_id,
        has_metadata=has_metadata,
        has_citations=has_citations,
        verify_id_existence=verify_id_existence,
        draft_name=clean_name or None
    )

    # ── save uploaded files ───────────────────────────────────────────────────
    # Names are kept (sanitized to their final path component by
    # ``save_uploaded_file``); when both uploads share a filename the
    # citations copy is prefixed so it cannot overwrite the metadata one.
    meta_filename = metadata_file.filename or 'metadata.csv' if has_metadata else None
    cits_filename = citations_file.filename or 'citations.csv' if has_citations else None
    if meta_filename and cits_filename \
            and Path(meta_filename).name == Path(cits_filename).name:
        cits_filename = 'cits_' + Path(cits_filename).name

    if has_metadata:
        meta_content = await metadata_file.read()
        if not meta_content:
            raise HTTPException(status_code=400, detail="Metadata file is empty.")
        _check_csv_header(meta_filename, meta_content)
        meta_path = await SessionManager.save_uploaded_file(
            session_id, meta_content, meta_filename, fallback='metadata.csv')
        session.meta_csv_path = meta_path

    if has_citations:
        cits_content = await citations_file.read()
        if not cits_content:
            raise HTTPException(status_code=400, detail="Citations file is empty.")
        _check_csv_header(cits_filename, cits_content)
        cits_path = await SessionManager.save_uploaded_file(
            session_id, cits_content, cits_filename, fallback='citations.csv')
        session.cits_csv_path = cits_path

    # ── validation + HTML generation ──────────────────────────────────────────
    try:
        if has_metadata and has_citations:
            # ── Paired validation via ClosureValidator ──────────────────────
            meta_is_valid, cits_is_valid, meta_report_path, cits_report_path = \
                ValidatorService.validate_pair(
                    meta_csv_path=session.meta_csv_path,
                    cits_csv_path=session.cits_csv_path,
                    meta_output_dir=str(session_dir),
                    cits_output_dir=str(session_dir),
                    verify_id_existence=verify_id_existence
                )

            session.meta_report_path = meta_report_path
            session.cits_report_path = cits_report_path

            # Generate each table (meta_table.html / cits_table.html — the
            # commit artifacts), then read back once for the baselines.
            meta_table_path = session_dir / 'meta_table.html'
            cits_table_path = session_dir / 'cits_table.html'

            ValidatorService.generate_table_html(
                session.meta_csv_path, meta_report_path,
                str(meta_table_path), meta_is_valid, 'Metadata')
            ValidatorService.generate_table_html(
                session.cits_csv_path, cits_report_path,
                str(cits_table_path), cits_is_valid, 'Citations')

            with open(meta_table_path, 'r', encoding='utf-8') as f:
                meta_html_content = f.read()
            with open(cits_table_path, 'r', encoding='utf-8') as f:
                cits_html_content = f.read()

            # Save baseline snapshots for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, meta_html_content, 'meta')
            await SessionManager.save_baseline_snapshot(session_id, cits_html_content, 'cits')

        elif has_metadata:
            # ── Single metadata table ───────────────────────────────────────
            meta_is_valid, meta_report_path = ValidatorService.validate_metadata(
                csv_path=session.meta_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )

            session.meta_report_path = meta_report_path

            meta_table_path = session_dir / 'meta_table.html'
            ValidatorService.generate_table_html(
                session.meta_csv_path, meta_report_path,
                str(meta_table_path), meta_is_valid, 'Metadata')

            with open(meta_table_path, 'r', encoding='utf-8') as f:
                meta_html_content = f.read()

            # Save baseline snapshot for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, meta_html_content, 'meta')

        elif has_citations:
            # ── Single citations table ──────────────────────────────────────
            cits_is_valid, cits_report_path = ValidatorService.validate_citations(
                csv_path=session.cits_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )

            session.cits_report_path = cits_report_path

            cits_table_path = session_dir / 'cits_table.html'
            ValidatorService.generate_table_html(
                session.cits_csv_path, cits_report_path,
                str(cits_table_path), cits_is_valid, 'Citations')

            with open(cits_table_path, 'r', encoding='utf-8') as f:
                cits_html_content = f.read()

            # Save baseline snapshot for deletion detection
            await SessionManager.save_baseline_snapshot(session_id, cits_html_content, 'cits')

        # Build the generation artifacts (one threaded parse per table —
        # becomes the immutable base for journal replay) and initialize one
        # change journal per uploaded table, against its own generation:
        # in paired sessions both tables are independently editable.
        table_types = (('meta', 'cits') if (has_metadata and has_citations)
                       else ('meta',) if has_metadata else ('cits',))
        generations = {}
        for tt in table_types:
            generations[tt] = await build_generation_artifacts(session_id, tt)
            journal = await ChangeJournal.load(session_id, table_type=tt)
            await journal.ensure_initialized(generations[tt], tt)

        # Mark as validated and persist session
        session.mark_validated()
        await SessionManager.save_session(session)

        return {
            "session_id": session_id,
            "html_url": f"/editor/{session_id}"
        }

    except TableNotMatchingInstance as e:
        # A table uploaded through the wrong input (e.g. citations data as
        # metadata): same error the paired ClosureValidator path raises.
        async with document_cache.session_lock(session_id):
            SessionManager.delete_session(session_id)
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        async with document_cache.session_lock(session_id):
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
        async with document_cache.session_lock(session_id):
            SessionManager.delete_session(session_id)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")
