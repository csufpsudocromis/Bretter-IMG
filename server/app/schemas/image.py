from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ImageOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    filename: str
    size_bytes: int
    os_version: Optional[str]
    architecture: str
    checksum_sha256: Optional[str]
    created_at: datetime
    created_by: Optional[str]
    source_machine_id: Optional[str]
    tags: Optional[str]

    class Config:
        from_attributes = True


class ImageUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    os_version: Optional[str] = None
    tags: Optional[str] = None
