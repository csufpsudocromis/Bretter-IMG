from sqlalchemy import Column, String, DateTime, Enum as SAEnum, Text, ForeignKey, Integer
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from ..core.database import Base


class JobType(str, enum.Enum):
    capture = "capture"
    deploy = "deploy"
    wipe = "wipe"
    inventory = "inventory"
    reboot = "reboot"
    shutdown = "shutdown"
    push_winpe = "push_winpe"
    update_agent = "update_agent"
    post_deploy = "post_deploy"
    remote_command = "remote_command"
    file_transfer = "file_transfer"


class JobStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    type = Column(SAEnum(JobType), nullable=False)
    status = Column(SAEnum(JobStatus), default=JobStatus.pending)
    machine_id = Column(String, ForeignKey("machines.id", ondelete="CASCADE"), nullable=False)
    image_id = Column(String)
    capture_name = Column(String)
    capture_method = Column(String, default="vss")   # "vss" or "winpe"
    scheduled_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    progress_percent = Column(Integer)
    log = Column(Text)
    created_by = Column(String)
    capture_smb_username = Column(String)
    capture_smb_password = Column(String)
    remove_winpe_after_deploy = Column(String, default="false")
    restore_hostname_after_deploy = Column(String, default="false")
    original_hostname = Column(String)
    domain_name = Column(String)
    domain_username = Column(String)
    domain_password = Column(String)
    sysprep_before_capture = Column(String, default="false")
    unattended_file_path = Column(String)
    parent_job_id = Column(String)
    command_shell = Column(String)
    command_text = Column(Text)
    command_timeout_seconds = Column(Integer)
    command_interactive = Column(String, default="false")
    transfer_target_path = Column(String)
    transfer_payload_name = Column(String)
    transfer_extract_archive = Column(String, default="true")
    created_at = Column(DateTime, default=datetime.utcnow)

    machine = relationship("Machine", back_populates="jobs", foreign_keys=[machine_id])
