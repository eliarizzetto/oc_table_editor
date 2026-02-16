"""OC Table Editor - Main application entry point."""
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import aiofiles

# Add parent directory to path to import oc_validator
parent_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(parent_dir))

from config import (
    SESSION_DIR,
    SESSION_CLEANUP_INTERVAL,
    SESSION_EXPIRY_HOURS,
    MAX_UPLOAD_SIZE,
    DEFAULT_VERIFY_ID_EXISTENCE
)
from services import SessionManager
from routes import router as api_router

# Lifespan for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    print("OC Table Editor starting up...")
    print(f"Session directory: {SESSION_DIR}")
    print(f"Upload size limit: {MAX_UPLOAD_SIZE / (1024*1024)} MB")
    print(f"Session expiry: {SESSION_EXPIRY_HOURS} hours")
    yield
    # Shutdown
    print("OC Table Editor shutting down...")


# Create FastAPI application
app = FastAPI(
    title="OC Table Editor",
    description="Web-based editor for validated bibliographic CSV tables",
    version="1.0.0",
    lifespan=lifespan
)

# Setup directories
SESSION_DIR.mkdir(parents=True, exist_ok=True)
Path(__file__).parent.parent / "oc_table_editor" / "static" / "css".mkdir(parents=True, exist_ok=True)
Path(__file__).parent.parent / "oc_table_editor" / "static" / "js".mkdir(parents=True, exist_ok=True)

# Setup templates
templates = Jinja2Templates(directory="templates")

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include API routes
app.include_router(api_router, prefix="/api")

# Page routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the upload page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/editor/{session_id}", response_class=HTMLResponse)
async def editor(request: Request, session_id: str):
    """Render the editor page for a session."""
    # Verify session exists
    session = await SessionManager.load_session(session_id)
    if not session:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": f"Session '{session_id}' not found or has expired."}
        )
    
    return templates.TemplateResponse(
        "editor.html",
        {"request": request, "session_id": session_id}
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": "OC Table Editor",
        "version": "1.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    
    print("=" * 60)
    print("OC Table Editor")
    print("=" * 60)
    print("Starting server on http://127.0.0.1:8000")
    print("Press Ctrl+C to stop")
    print("=" * 60)
    
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info"
    )