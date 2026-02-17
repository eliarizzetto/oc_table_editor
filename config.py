"""
Configuration constants for the OC Table Editor application.
"""
import os
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"

# File upload settings
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB in bytes
ALLOWED_EXTENSIONS = {'.csv'}

# Validation settings
DEFAULT_VERIFY_ID_EXISTENCE = False  # Default: no external API calls

# Session settings
SESSION_DIR = TEMP_DIR
SESSION_CLEANUP_INTERVAL = 3600  # 1 hour in seconds
SESSION_EXPIRY_HOURS = 24
SESSION_TIMEOUT = None  # Sessions persist indefinitely (drafts)