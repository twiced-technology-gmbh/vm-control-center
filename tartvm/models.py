"""Data models for VM Control Center."""
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class VMStatus(str, Enum):
    """VM status enum."""
    RUNNING = "running"
    STOPPED = "stopped"
    CREATING = "creating"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VMModel(BaseModel):
    """VM model representing an Orchard-managed VM."""
    name: str
    status: VMStatus = VMStatus.UNKNOWN
    os: Optional[str] = None
    worker: Optional[str] = None
    image: Optional[str] = None
    cpu: Optional[int] = None
    memory: Optional[int] = None
    disk_size: Optional[int] = None
    restart_policy: Optional[str] = None
    created_at: Optional[str] = None


class TaskStatus(str, Enum):
    """Task status enum."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskModel(BaseModel):
    """Task model for background operations."""
    id: str
    action: str
    status: TaskStatus = TaskStatus.PENDING
    command: Optional[List[str]] = None
    exit_code: Optional[int] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    stderr: Optional[str] = None
    created_at: float = Field(default_factory=lambda: time.time())
    updated_at: float = Field(default_factory=lambda: time.time())
    logs: List[str] = Field(default_factory=list)


class VMSummary(BaseModel):
    """Summary of VMs."""
    total: int = 0
    running: int = 0
    stopped: int = 0
    unknown: int = 0


class VMImageModel(BaseModel):
    """Model for available VM images."""
    name: str
    url: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    updated_at: Optional[str] = None


class TartImageModel(BaseModel):
    """Model for local Tart images (source images + OCI cache)."""
    name: str
    source: str  # "local" or "OCI"
    disk_size: Optional[int] = None
    size: Optional[int] = None
    os: Optional[str] = None
    last_accessed: Optional[str] = None
    used_by: List[str] = Field(default_factory=list)


class CreateVMRequest(BaseModel):
    """Request model for creating a VM via Orchard."""
    name: str
    image: str = "ghcr.io/cirruslabs/macos-tahoe-base:latest"
    cpu: int = 4
    memory: int = 8192
    disk_size: Optional[int] = None
    startup_script: Optional[str] = None
