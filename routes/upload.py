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
    # Check file sizes and validate files exist with content
    has_metadata = False
    has_citations = False
    
    if metadata_file:
        metadata_file.file.seek(0, 2)  # Seek to end
        size = metadata_file.file.tell()
        metadata_file.file.seek(0)  # Reset
        
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
    
    # Validate that at least one file is provided
    if not has_metadata and not has_citations:
        raise HTTPException(status_code=400, detail="At least one CSV file must be provided")
    
    # Create session
    session_id = SessionManager.create_session_id()
    session_dir = SessionManager.create_session_dir(session_id)
    
    # Create session object
    session = Session(
        session_id=session_id,
        has_metadata=has_metadata,
        has_citations=has_citations,
        verify_id_existence=verify_id_existence
    )
    
    # Save uploaded files
    if has_metadata:
        # Use provided filename or default to 'metadata.csv'
        filename = metadata_file.filename or 'metadata.csv'
        if not filename:
            raise HTTPException(
                status_code=400,
                detail="Metadata filename is required"
            )
        meta_content = await metadata_file.read()
        
        # Validate file content is not empty
        if not meta_content:
            raise HTTPException(
                status_code=400,
                detail="Metadata file is empty. Please upload a valid CSV file."
            )
        
        meta_path = await SessionManager.save_uploaded_file(
            session_id, meta_content, filename
        )
        session.meta_csv_path = meta_path
    
    if has_citations:
        # Use provided filename or default to 'citations.csv'
        filename = citations_file.filename or 'citations.csv'
        if not filename:
            raise HTTPException(
                status_code=400,
                detail="Citations filename is required"
            )
        cits_content = await citations_file.read()
        
        # Validate file content is not empty
        if not cits_content:
            raise HTTPException(
                status_code=400,
                detail="Citations file is empty. Please upload a valid CSV file."
            )
        
        cits_path = await SessionManager.save_uploaded_file(
            session_id, cits_content, filename
        )
        session.cits_csv_path = cits_path
    
    # Run validation
    try:
        if has_metadata and has_citations:
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
            
            # Validate report paths were found
            if not session.meta_report_path:
                raise ValueError(
                    f"Metadata validation report not found. Expected in: {session_dir}"
                )
            if not session.cits_report_path:
                raise ValueError(
                    f"Citations validation report not found. Expected in: {session_dir}"
                )
            
            # Generate HTML for both tables
            meta_html_path = session_dir / 'meta_html.html'
            cits_html_path = session_dir / 'cits_html.html'
            
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
            
        elif has_metadata:
            # Validate metadata only
            meta_report = ValidatorService.validate_metadata(
                csv_path=session.meta_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )
            
            session.meta_report_path = ValidatorService.get_report_json_path(
                session.meta_csv_path, str(session_dir)
            )
            
            # Validate report path was found
            if not session.meta_report_path:
                raise ValueError(
                    f"Metadata validation report not found. Expected in: {session_dir}"
                )
            
            # Generate HTML
            meta_html_path = session_dir / 'meta_html.html'
            make_gui(
                session.meta_csv_path,
                session.meta_report_path,
                str(meta_html_path)
            )
            
            session.meta_html_path = str(meta_html_path)
            
        elif has_citations:
            # Validate citations only
            cits_report = ValidatorService.validate_citations(
                csv_path=session.cits_csv_path,
                output_dir=str(session_dir),
                verify_id_existence=verify_id_existence
            )
            
            session.cits_report_path = ValidatorService.get_report_json_path(
                session.cits_csv_path, str(session_dir)
            )
            
            # Validate report path was found
            if not session.cits_report_path:
                raise ValueError(
                    f"Citations validation report not found. Expected in: {session_dir}"
                )
            
            # Generate HTML
            cits_html_path = session_dir / 'cits_html.html'
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
        
    except ValueError as e:
        # Clean up on error
        SessionManager.delete_session(session_id)
        # Log the error for debugging
        import traceback
        traceback.print_exc()
        
        # Provide user-friendly error message
        error_msg = str(e)
        if "delimiter" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="Invalid CSV file format. Please ensure the file is a valid CSV with proper delimiters (comma, semicolon, or tab)."
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"CSV validation error: {error_msg}"
            )
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Clean up on error
        SessionManager.delete_session(session_id)
        # Log the error for debugging
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Validation failed: {str(e)}"
        )
