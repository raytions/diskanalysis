from datetime import datetime
from typing import Any
from pydantic import BaseModel


class MountSchema(BaseModel):
    id: int
    mount_point: str
    device: str
    fs_type: str
    is_network_fs: bool
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float
    is_accessible: bool
    created_at: datetime
    updated_at: datetime
    last_scanned_at: datetime | None
    label: str = ""

    model_config = {"from_attributes": True}


class ScanSchema(BaseModel):
    id: int
    mount_id: int | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    scan_mode: str
    target_path: str | None
    filesystem_boundary_only: bool
    exclude_rules: str | None
    cold_days: int
    top_n: int
    total_files: int
    total_dirs: int
    total_bytes: int
    duration_seconds: float | None
    error_count: int
    error_summary: str | None
    worker_pid: int | None = None

    model_config = {"from_attributes": True}


class FileSchema(BaseModel):
    id: int
    scan_id: int
    mount_id: int | None
    path: str
    size_bytes: int
    extension: str | None
    atime: float | None
    mtime: float | None
    ctime: float | None
    is_symlink: bool
    is_cold: bool
    cold_days_bucket: int | None

    model_config = {"from_attributes": True}


class DirectorySchema(BaseModel):
    id: int
    scan_id: int
    mount_id: int | None
    path: str
    total_size_bytes: int
    file_count: int
    cold_file_count: int
    cold_total_size_bytes: int

    model_config = {"from_attributes": True}


class ScanErrorSchema(BaseModel):
    id: int
    scan_id: int
    path: str | None
    error_type: str
    error_message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtensionSummaryItem(BaseModel):
    extension: str
    file_count: int
    total_size_bytes: int


class ScanCreateRequest(BaseModel):
    scan_mode: str = "all_mounts"  # all_mounts | selected_mounts | single_mount | path
    scan_type: str = "full"
    mount_id: int | None = None
    mount_ids: list[int] | None = None  # for selected_mounts mode
    target_path: str | None = None
    filesystem_boundary_only: bool = True
    exclude_rules: str | None = None
    cold_days: int = 90
    top_n: int = 100


class PaginatedResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Any]


def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def err(message: str, code: int = 400) -> dict:
    return {"ok": False, "error": message, "code": code}
