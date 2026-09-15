from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..models.machine import MachineStatus


class MachineRegister(BaseModel):
    id: str
    hostname: str
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    os_version: Optional[str] = None
    cpu: Optional[str] = None
    ram_gb: Optional[str] = None
    disk_info: Optional[str] = None
    agent_version: Optional[str] = None


class MachineHeartbeat(BaseModel):
    status: MachineStatus = MachineStatus.online


class MachineUpdate(BaseModel):
    notes: Optional[str] = None
    status: Optional[MachineStatus] = None
    group_id: Optional[str] = None


class MachineOut(BaseModel):
    id: str
    hostname: str
    ip_address: Optional[str]
    mac_address: Optional[str]
    os_version: Optional[str]
    cpu: Optional[str]
    ram_gb: Optional[str]
    disk_info: Optional[str]
    status: MachineStatus
    last_seen: Optional[datetime]
    registered_at: Optional[datetime]
    agent_version: Optional[str]
    current_agent_version: Optional[str] = None
    agent_update_available: bool = False
    notes: Optional[str]
    group_id: Optional[str]

    class Config:
        from_attributes = True


class MachineRegisterOut(MachineOut):
    agent_token: Optional[str] = None
