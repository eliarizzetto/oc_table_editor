"""Session and edit tracking models."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional
import json


@dataclass
class EditState:
    """Track the state of a single edited item."""
    item_id: str
    original_value: str
    edited_value: str
    edited: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'item_id': self.item_id,
            'original_value': self.original_value,
            'edited_value': self.edited_value,
            'edited': self.edited,
            'timestamp': self.timestamp
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'EditState':
        """Create instance from dictionary."""
        return cls(
            item_id=data['item_id'],
            original_value=data['original_value'],
            edited_value=data['edited_value'],
            edited=data.get('edited', False),
            timestamp=data.get('timestamp', datetime.now().isoformat())
        )


@dataclass
class Session:
    """Session data for tracking a user's editing session."""
    session_id: str
    has_metadata: bool = False
    has_citations: bool = False
    verify_id_existence: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # File paths
    meta_csv_path: Optional[str] = None
    cits_csv_path: Optional[str] = None
    meta_report_path: Optional[str] = None
    cits_report_path: Optional[str] = None
    meta_html_path: Optional[str] = None
    cits_html_path: Optional[str] = None
    edit_state_path: Optional[str] = None
    
    # Validation status
    last_validated_at: Optional[str] = None
    has_edits_since_validation: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'session_id': self.session_id,
            'has_metadata': self.has_metadata,
            'has_citations': self.has_citations,
            'verify_id_existence': self.verify_id_existence,
            'created_at': self.created_at,
            'last_updated': self.last_updated,
            'meta_csv_path': self.meta_csv_path,
            'cits_csv_path': self.cits_csv_path,
            'meta_report_path': self.meta_report_path,
            'cits_report_path': self.cits_report_path,
            'meta_html_path': self.meta_html_path,
            'cits_html_path': self.cits_html_path,
            'edit_state_path': self.edit_state_path,
            'last_validated_at': self.last_validated_at,
            'has_edits_since_validation': self.has_edits_since_validation
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Session':
        """Create instance from dictionary."""
        return cls(
            session_id=data['session_id'],
            has_metadata=data.get('has_metadata', False),
            has_citations=data.get('has_citations', False),
            verify_id_existence=data.get('verify_id_existence', False),
            created_at=data.get('created_at', datetime.now().isoformat()),
            last_updated=data.get('last_updated', datetime.now().isoformat()),
            meta_csv_path=data.get('meta_csv_path'),
            cits_csv_path=data.get('cits_csv_path'),
            meta_report_path=data.get('meta_report_path'),
            cits_report_path=data.get('cits_report_path'),
            meta_html_path=data.get('meta_html_path'),
            cits_html_path=data.get('cits_html_path'),
            edit_state_path=data.get('edit_state_path'),
            last_validated_at=data.get('last_validated_at'),
            has_edits_since_validation=data.get('has_edits_since_validation', False)
        )
    
    def update_timestamp(self):
        """Update the last_updated timestamp."""
        self.last_updated = datetime.now().isoformat()
    
    def mark_edited(self):
        """Mark that edits have been made since last validation."""
        self.has_edits_since_validation = True
        self.update_timestamp()
    
    def mark_validated(self):
        """Mark that validation has been run (clears edit flag)."""
        self.last_validated_at = datetime.now().isoformat()
        self.has_edits_since_validation = False
        self.update_timestamp()