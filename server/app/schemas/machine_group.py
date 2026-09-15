from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MachineGroupCreate(BaseModel):
    name: str
    description: Optional[str] = None


class MachineGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class MachineGroupAssign(BaseModel):
    machine_ids: list[str]


class MachineGroupOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    created_at: datetime
    machine_count: int = 0

    class Config:
        from_attributes = True
