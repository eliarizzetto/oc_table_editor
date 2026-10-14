"""
Configuration constants for the OC Table Editor application.
"""
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"

# File upload settings
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB in bytes

# Validation settings
DEFAULT_VERIFY_ID_EXISTENCE = False  # Default: no external API calls

# Session settings
SESSION_DIR = TEMP_DIR  # sessions persist indefinitely (user-managed drafts)
MAX_DRAFT_NAME_LEN = 120  # shared by upload + rename validation

# Table view pagination
TABLE_PAGE_SIZE = 25  # max rows per page in the editor's main/filtered views


# version
VERSION = "1.0.0"
