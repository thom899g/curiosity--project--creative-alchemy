"""
Main orchestrator for Creative Alchemy system.
Manages state, idempotency, and pipeline coordination via Firebase Firestore.
"""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
from dataclasses import dataclass, asdict

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter
from loguru import logger

from config import settings

class PipelineStage(str, Enum):
    """Pipeline stages for state tracking."""
    TRIGGER_DETECTED = "trigger_detected"
    THEMATIC_GENERATED = "thematic_generated"
    ASSETS_CREATED = "assets_created"
    ASSETS_SCORED = "assets_scored"
    AWAITING_CURATION = "awaiting_curation"
    CURATION_COMPLETE = "curation_complete"
    SAFETY_CHECKED = "safety_checked"
    READY_TO_MINT = "ready_to_mint"
    MINTING = "mining"
    MINTED = "minted"
    FAILED = "failed"

@dataclass
class PipelineState:
    """Immutable state container for pipeline execution."""
    trigger_id: str
    stage: PipelineStage
    created_at: datetime
    updated_at: datetime
    data: Dict[str, Any]
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    
    def to_firestore_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary."""
        return {
            'trigger_id': self.trigger_id,
            'stage': self.stage.value,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'data': json.dumps(self.data, default=str),
            'error': self.error,
            'retry_count': self.retry_count,
            'max_retries': self.max_retries
        }
    
    @classmethod
    def from_firestore_dict(cls, data: Dict[str, Any]) -> 'PipelineState':
        """Create from Firestore dictionary."""
        return cls(
            trigger_id=data['trigger_id'],
            stage=PipelineStage(data['stage']),
            created_at=data['created_at'],
            updated_at=data['updated_at'],
            data=json.loads(data['data']),
            error=data.get('error'),
            retry_count=data.get('retry_count', 0),
            max_retries=data.get('max_retries', 3)
        )

class Conductor:
    """Main orchestrator managing pipeline state and idempotency."""
    
    def __init__(self):
        """Initialize Firebase connection and state management."""
        self.db: Optional[FirestoreClient] = None
        self._initialized = False
        self._kill_switch_listener = None
        self._pending_tasks: Dict[str, asyncio.Task] = {}
        
    def initialize(self) -> None:
        """Initialize Firebase connection with error handling."""
        if self._initialized:
            return
            
        try:
            # Load credentials
            creds_dict = settings.load_firebase_credentials()
            cred = credentials.Certificate(creds_dict)
            
            # Initialize Firebase if not already initialized
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred, {
                    'projectId': settings.firebase_project_id or creds_dict.get('project_id')
                })
            
            self.db = firestore.client()
            self._initialized = True
            
            # Create collections if they don't exist (Firestore is schemaless, but we ensure structure)
            self._ensure_collections()
            
            # Setup kill switch listener
            self._setup_kill_switch_listener()
            
            logger.success("Conductor initialized successfully")
            
        except FileNotFoundError as e:
            logger.error(f"Firebase credentials not found: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize conductor: {e}")
            raise
    
    def _ensure_collections(self) -> None:
        """Ensure required collections exist with sample documents."""
        collections = ['Triggers', 'Assets', 'Chronicles', 'MarketFeedback', 'PipelineStates']
        
        for collection_name in collections:
            # Firestore creates collections on first write, but we can verify access
            try:
                # Try to read a non-existent document to verify collection access
                doc_ref = self.db.collection(collection_name).document('test')
                doc_ref.get()
            except Exception as e:
                logger.warning(f"Collection {collection_name} access check failed: {e}")
    
    def _setup_kill_switch_listener(self) -> None:
        """Setup real-time listener for kill switch."""
        def on_kill_switch_snapshot(doc_snapshot, changes, read_time):
            for doc in doc_snapshot:
                data = doc.to_dict()
                if data.get('enabled', False):
                    logger.critical("KILL SWITCH ACTIVATED! Halting all operations.")
                    self._shutdown_pipeline()
        
        if self.db:
            self._kill_switch_listener = (
                self.db.collection('Settings')
                .document('kill_switch')
                .on_snapshot(on_kill_switch_snapshot)
            )
    
    def _shutdown_pipeline(self) -> None:
        """Gracefully shutdown pipeline tasks."""
        for task_id, task in self._pending_tasks.items():
            if not task.done():
                task.cancel()
                logger.info(f"Cancelled task: {task_id}")
    
    def generate_trigger_id(self, source_data: Dict[str, Any]) -> str:
        """
        Generate deterministic trigger ID for idempotency.
        
        Args:
            source_data: Dictionary containing trigger source data
            
        Returns:
            SHA256 hash of canonical JSON representation
        """
        # Create canonical representation
        canonical = {
            'timestamp': int(time.time() // 60) * 60,  # Bucket by minute for idempotency
            'project_path': source_data.get('project_path', ''),
            'commit_hash': source_data.get('commit_hash', ''),
            'trigger_type': source_data.get('trigger_type', 'unknown')
        }
        
        # Sort keys for consistent hashing
        canonical_str = json.dumps(canonical, sort_keys=True)
        
        return hashlib.sha256(canonical_str.encode()).hexdigest()[:32]
    
    def check_duplicate_trigger(self, trigger_id: str) -> bool:
        """
        Check if trigger has already been processed.
        
        Args:
            trigger_id: Unique trigger identifier
            
        Returns:
            True if duplicate exists, False otherwise
        """
        if not self.db:
            raise RuntimeError("Conductor not initialized")
        
        try:
            # Check in Triggers collection
            trigger_ref = self.db.collection('Triggers').document(trigger_id)
            trigger_doc = trigger_ref.get()
            
            # Check in PipelineStates collection
            pipeline_ref = self.db.collection('PipelineStates').where(
                FieldFilter('trigger_id', '==', trigger_id)
            ).limit(1).get()
            
            return trigger_doc.exists or len(pipeline_ref) > 0
            
        except Exception as e:
            logger.error(f"Error checking duplicate trigger: {e}")
            # In case of error, assume not duplicate to avoid missing triggers
            return False
    
    def save_trigger(self, trigger_data: Dict[str, Any]) -> str:
        """
        Save trigger to Firestore with idempotency check.
        
        Args:
            trigger_data: Trigger information dictionary
            
        Returns:
            trigger_id if saved, empty string if duplicate
        """
        if not self.db:
            raise RuntimeError("Conductor not initialized")
        
        trigger_id = self.generate_trigger_id(trigger_data)
        
        # Check for duplicates
        if self.check_duplicate_trigger(trigger_id):
            logger.info(f"Duplicate trigger detected: {trigger_id}")
            return ""
        
        try:
            # Add metadata
            trigger_data['trigger_id'] = trigger_id
            trigger_data['created_at'] = firestore.SERVER_TIMESTAMP
            trigger_data['processed'] = False
            
            # Save to Firestore
            trigger_ref = self.db.collection('Triggers').document(trigger_id)
            trigger_ref.set(trigger_data)
            
            logger.info(f"Saved trigger: {trigger_id} - {trigger_data.get('trigger_type')}")
            return trigger_id
            
        except Exception as e:
            logger.error(f"Failed to save trigger: {e}")
            return ""
    
    def create_pipeline_state(self, trigger_id: str, initial_data: Dict[str, Any]) -> Optional[str]:
        """
        Create new pipeline state for a trigger.
        
        Args:
            trigger_id: Unique trigger identifier
            initial_data: Initial pipeline data
            
        Returns:
            State document ID or None if failed
        """
        if not self.db:
            raise RuntimeError("Conductor not initialized")
        
        try:
            state = PipelineState(
                trigger_id=trigger_id,
                stage=PipelineStage.TRIGGER_DETECTED,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                data=initial_data
            )
            
            state_ref = self