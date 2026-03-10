"""
Configuration management for Creative Alchemy system.
Uses Pydantic for validation and environment variable loading.
"""
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, validator
from pydantic_settings import BaseSettings
import json

class PathConfig(BaseModel):
    """Validated path configuration."""
    project_watch_paths: List[Path] = Field(default=["./projects", Path.home() / "code"])
    git_repo_paths: List[Path] = Field(default=["./projects"])
    asset_output_dir: Path = Field(default=Path("./generated_assets"))
    firebase_service_account: Path = Field(default=Path("./serviceAccountKey.json"))
    
    @validator('project_watch_paths', 'git_repo_paths', each_item=True, pre=True)
    def expand_paths(cls, v):
        """Expand user and environment variables in paths."""
        return Path(os.path.expanduser(os.path.expandvars(v)))
    
    @validator('asset_output_dir', 'firebase_service_account')
    def ensure_dir_exists(cls, v):
        """Create directories if they don't exist."""
        if v.is_dir():
            v.mkdir(parents=True, exist_ok=True)
        return v

class ModelConfig(BaseModel):
    """Local LLM configuration."""
    model_name: str = Field(default="microsoft/phi-3-mini-4k-instruct")
    device: str = Field(default="cuda")
    precision: str = Field(default="float16")
    max_length: int = Field(default=512)
    temperature: float = Field(default=0.7)
    
    @validator('device')
    def validate_device(cls, v):
        """Validate CUDA availability."""
        if v == "cuda":
            try:
                import torch
                if not torch.cuda.is_available():
                    print("CUDA not available, falling back to CPU")
                    return "cpu"
            except ImportError:
                return "cpu"
        return v

class CurationConfig(BaseModel):
    """Curation and quality control configuration."""
    curation_window_hours: int = Field(default=24, ge=1, le=168)
    max_generation_per_trigger: int = Field(default=100, ge=10, le=1000)
    min_aesthetic_score: float = Field(default=0.7, ge=0.0, le=1.0)
    human_approval_required: bool = Field(default=True)

class SafetyConfig(BaseModel):
    """Safety and rate limiting configuration."""
    max_daily_mints: int = Field(default=10, ge=1, le=100)
    gas_price_threshold_gwei: int = Field(default=15, ge=1, le=1000)
    enable_nsfw_filter: bool = Field(default=True)
    enable_text_filter: bool = Field(default=True)
    kill_switch_enabled: bool = Field(default=False)

class Settings(BaseSettings):
    """Main settings class integrating all configurations."""
    # Firebase
    firebase_project_id: Optional[str] = None
    
    # Config sections
    paths: PathConfig = Field(default_factory=PathConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    curation: CurationConfig = Field(default_factory=CurationConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    
    # Derived from environment
    log_level: str = Field(default="INFO")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        env_nested_delimiter = "__"
    
    def load_firebase_credentials(self) -> dict:
        """Load Firebase service account credentials."""
        if not self.paths.firebase_service_account.exists():
            raise FileNotFoundError(
                f"Firebase service account file not found: {self.paths.firebase_service_account}"
            )
        
        with open(self.paths.firebase_service_account, 'r') as f:
            return json.load(f)

# Global settings instance
settings = Settings()