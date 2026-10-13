from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ConvertRequest(BaseModel):
    source_path: str
    target_format: str
    naming_rule: Optional[str] = "{title}.{author}.{ext}"
    keep_source: bool = False
    use_original_name: bool = False
    target_dir: Optional[str] = "temp"


class BatchConvertRequest(BaseModel):
    files: list[ConvertRequest]


class RenamePreview(BaseModel):
    file_path: str
    new_name: str


class RenameRequest(BaseModel):
    file_path: str
    new_name: str
    dry_run: bool = True


class BatchRenameRequest(BaseModel):
    files: list[RenameRequest]
    dry_run: bool = True


class FileInfo(BaseModel):
    path: str
    name: str
    ext: str
    size: int
    is_dir: bool
    modified: Optional[str] = None


class ConvertTask(BaseModel):
    task_id: str
    source_path: str
    target_format: str
    status: str  # pending / running / success / failed
    progress: float
    error: Optional[str] = None
    output_path: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
