from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from ..models.job import JobType, JobStatus


class JobCreate(BaseModel):
    type: JobType
    machine_ids: List[str]
    image_id: Optional[str] = None
    capture_name: Optional[str] = None
    capture_method: Optional[str] = "vss"   # "vss" or "winpe"
    scheduled_at: Optional[datetime] = None
    remove_winpe_after_deploy: bool = False
    restore_hostname_after_deploy: bool = False
    domain_name: Optional[str] = None
    domain_username: Optional[str] = None
    domain_password: Optional[str] = None
    sysprep_before_capture: bool = False
    unattended_file_path: Optional[str] = None
    command_shell: Optional[str] = None
    command_text: Optional[str] = None
    command_timeout_seconds: Optional[int] = 300
    command_interactive: bool = False
    transfer_target_path: Optional[str] = None


class JobStatusUpdate(BaseModel):
    status: JobStatus
    log: Optional[str] = None


class JobOut(BaseModel):
    id: str
    type: JobType
    status: JobStatus
    machine_id: str
    image_id: Optional[str]
    capture_name: Optional[str]
    capture_method: Optional[str]
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    progress_percent: Optional[int] = None
    log: Optional[str]
    created_by: Optional[str]
    parent_job_id: Optional[str] = None
    command_shell: Optional[str] = None
    command_text: Optional[str] = None
    command_timeout_seconds: Optional[int] = None
    command_interactive: Optional[str] = None
    transfer_target_path: Optional[str] = None
    transfer_payload_name: Optional[str] = None
    transfer_extract_archive: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AgentJobOut(JobOut):
    direct_capture_username: Optional[str] = None
    direct_capture_password: Optional[str] = None
    deploy_image_filename: Optional[str] = None
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None
    remove_winpe_after_deploy: bool = False
    restore_hostname_after_deploy: bool = False
    original_hostname: Optional[str] = None
    domain_name: Optional[str] = None
    domain_username: Optional[str] = None
    domain_password: Optional[str] = None
    sysprep_before_capture: bool = False
    unattended_file_path: Optional[str] = None
    command_shell: Optional[str] = None
    command_text: Optional[str] = None
    command_timeout_seconds: Optional[int] = None
    command_interactive: bool = False
    transfer_target_path: Optional[str] = None
    transfer_payload_name: Optional[str] = None
    transfer_extract_archive: Optional[str] = None
