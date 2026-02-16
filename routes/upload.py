"""Upload and validation routes."""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import shutil
import sys
from pathlib import Path

from services import ValidatorService, SessionManager
from models import Session
from config import MAX_UPLOAD_SIZE, DEFAULT_VERIFY_ID_EXISTENCE

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(parent_dir))

# Import oc_validator interface for HTML generation
from oc_validator.interface.gui import make_gui, merge_html_files

router = APIRouter()


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
    # Validate that at least one file is provided
    if not metadata_file and not citations_file:
        raise HTTPException(status_code=400, detail="At least one CSV file must be provided")
    
    # Check file sizes
    if metadata_file:
        metadata_file.file.seek(0, 2)  # Seek to end
        size = metadata_file.file.tell()
        metadata_file.file.seek(0)  # Reset
        
        if size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Metadata file exceeds maximum size of {MAX_UPLOAD_SIZE / (1024*1024)} MB"
            )
    
    if citations_file:
        citations_file.file.seek(0, 2)
        size = citations_file.file.tell()
        citations_file.file.seek(0)
        
        if size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Citations file exceeds maximum size of {MAX_UPLOAD_SIZE / (1024*1024)} MB"
            )
    
    # Create session
    session_id = SessionManager.create_session_id()
    session_dir = SessionManager.create_session_dir(session_id)
    
    # Create session object
    session = Session(
        session_id=session_id,
        has_metadata=metadata_file is not None,
        has_citations=citations_file is not None,
        verify_id_existence=verify_id_existence
    )
    
    # Save uploaded files
    if metadata_file:
        meta_content = await metadata_file.read()
        meta_path = await SessionManager.save_uploaded_file(
            session_id, meta_content, metadata_file.filename
        )
        session.meta_csv_path = meta_path
    
    if citations_file:
        cits_content = await citations_file.read()
        cits_path = await SessionManager.save_uploaded_file(
            session_id, cits_content, citations_file.filename
        )
        session.cits_csv_path = cits_path
    
    # Run validation
    try:
        if metadata_file and citations_file:
            # Validate paired files
            meta_report, cits_report = ValidatorService.validate_pair(
                meta_csv_path=session.meta_csv_path,
                cits_csv_path=session.cits_csv_path,
                meta_output_dir=str(session_dir),
                cits_output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )
            
            # Get report paths
            session.meta_report_path = ValidatorService.get_report_json_path(
                session.meta_csv_path, str(session_dir)
            )
            session.cits_report_path = ValidatorService.get_report_json_path(
                session.cits_csv_path, str(session_dir)
            )
            
            # Generate HTML for both tables
            meta_html_path = session_dir / 'meta.html'
            cits_html_path = session_dir / 'cits.html'
            
            make_gui(
                session.meta_csv_path,
                session.meta_report_path,
                str(meta_html_path)
            )
            make_gui(
                session.cits_csv_path,
                session.cits_report_path,
                str(cits_html_path)
            )
            
            # Merge HTML files
            merged_html_path = session_dir / 'merged.html'
            merge_html_files(
                str(meta_html_path),
                str(cits_html_path),
                str(merged_html_path)
            )
            
            session.meta_html_path = str(merged_html_path)
            
        elif metadata_file:
            # Validate metadata only
            meta_report = ValidatorService.validate_metadata(
                csv_path=session.meta_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )
            
            session.meta_report_path = ValidatorService.get_report_json_path(
                session.meta_csv_path, str(session_dir)
            )
            
            # Generate HTML
            meta_html_path = session_dir / 'meta.html'
            make_gui(
                session.meta_csv_path,
                session.meta_report_path,
                str(meta_html_path)
            )
            
            session.meta_html_path = str(meta_html_path)
            
        elif citations_file:
            # Validate citations only
            cits_report = ValidatorService.validate_citations(
                csv_path=session.cits_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )
            
            session.cits_report_path = ValidatorService.get_report_json_path(
                session.cits_csv_path, str(session_dir)
            )
            
            # Generate HTML
            cits_html_path = session_dir / 'cits.html'
            make_gui(
                session.cits_csv_path,
                session.cits_report_path,
                str(cits_html_path)
            )
            
            session.cits_html_path = str(cits_html_path)
        
        # Mark as validated
        session.mark_validated()
        
        # Save session
        await SessionManager.save_session(session)
        
        return {
            "session_id": session_id,
            "has_metadata": session.has_metadata,
            "has_citations": session.has_citations,
            "verify_id_existence": verify_id_existence,
            "html_url": f"/editor/{session_id}"
        }
        
    except Exception as e:
        # Clean up on error
        SessionManager.delete_session(session_id)
        raise HTTPException(
            status_code=500,
            detail=f"Validation failed: {str(e)}"
        )