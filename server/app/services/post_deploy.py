from datetime import datetime, timedelta
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from ..models.job import Job, JobStatus, JobType
from ..models.machine import Machine

FIRST_BOOT_POST_DEPLOY_DELAY = timedelta(minutes=5)
POST_DEPLOY_REBOOT_RECOVERY_DELAY = timedelta(seconds=90)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _deploy_requests_post_deploy(job: Job) -> bool:
    return (
        _truthy(job.restore_hostname_after_deploy)
        or bool(job.domain_name)
        or _truthy(job.remove_winpe_after_deploy)
    )


def _deploy_looks_applied(job: Job, machine: Machine) -> bool:
    if not job.started_at:
        return False
    if _truthy(job.restore_hostname_after_deploy) and job.original_hostname:
        if machine.hostname and machine.hostname.lower() != job.original_hostname.lower():
            return True
    log = job.log or ""
    if "Image applied successfully" in log or "Waiting for automatic post-deploy tasks to complete" in log:
        return True
    if job.progress_percent and job.progress_percent >= 100:
        return True
    return False


def ensure_post_deploy_for_deploy(db: Session, deploy: Job, machine: Machine) -> Optional[Job]:
    if not _deploy_requests_post_deploy(deploy):
        return None
    existing = (
        db.query(Job)
        .filter(
            Job.parent_job_id == deploy.id,
            Job.type == JobType.post_deploy,
        )
        .first()
    )
    if existing:
        return existing
    if not _deploy_looks_applied(deploy, machine):
        return None
    child = Job(
        id=str(uuid.uuid4()),
        type=JobType.post_deploy,
        status=JobStatus.pending,
        machine_id=deploy.machine_id,
        capture_method="vss",
        sysprep_before_capture="false",
        created_by="system",
        scheduled_at=datetime.utcnow() + FIRST_BOOT_POST_DEPLOY_DELAY,
        parent_job_id=deploy.id,
        remove_winpe_after_deploy=deploy.remove_winpe_after_deploy,
        restore_hostname_after_deploy=deploy.restore_hostname_after_deploy,
        original_hostname=deploy.original_hostname,
        domain_name=deploy.domain_name,
        domain_username=deploy.domain_username,
        domain_password=deploy.domain_password,
    )
    deploy.log = (deploy.log or "") + (
        f"Queued automatic post-deploy job {child.id} after image apply; "
        "waiting 5 minutes for Windows first-boot setup before running post-deploy tasks.\n"
    )
    db.add(child)
    db.flush()
    return child


def ensure_post_deploy_job(db: Session, machine: Machine) -> Optional[Job]:
    running_deploys = (
        db.query(Job)
        .filter(
            Job.machine_id == machine.id,
            Job.type == JobType.deploy,
            Job.status == JobStatus.running,
        )
        .order_by(Job.created_at.desc())
        .all()
    )
    for deploy in running_deploys:
        child = ensure_post_deploy_for_deploy(db, deploy, machine)
        if child:
            return child
    return None


def recover_post_deploy_after_reboot(db: Session, machine: Machine) -> Optional[Job]:
    """Requeue a post-deploy child that paused for a hostname reboot."""
    job = (
        db.query(Job)
        .filter(
            Job.machine_id == machine.id,
            Job.type == JobType.post_deploy,
            Job.status == JobStatus.running,
            Job.completed_at == None,
        )
        .order_by(Job.created_at.desc())
        .first()
    )
    if not job or not job.started_at:
        return None
    log_text = job.log or ""
    if "restarting before joining domain" not in log_text.lower():
        return None
    if datetime.utcnow() - job.started_at < POST_DEPLOY_REBOOT_RECOVERY_DELAY:
        return None

    job.status = JobStatus.pending
    job.scheduled_at = datetime.utcnow()
    job.log = log_text + "Machine checked in after hostname reboot; resuming domain join step.\n"
    return job


def complete_parent_deploy_from_post_deploy(db: Session, job: Job):
    if job.type != JobType.post_deploy or not job.parent_job_id:
        return
    parent = db.query(Job).filter(Job.id == job.parent_job_id).first()
    if not parent or parent.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        return
    if job.status == JobStatus.completed:
        parent.status = JobStatus.completed
        parent.completed_at = datetime.utcnow()
        parent.progress_percent = 100
        parent.log = (parent.log or "") + f"Automatic post-deploy job {job.id} completed successfully.\n"
    elif job.status in (JobStatus.failed, JobStatus.cancelled):
        parent.status = JobStatus.failed
        parent.completed_at = datetime.utcnow()
        parent.log = (parent.log or "") + f"Automatic post-deploy job {job.id} ended with status {job.status}.\n"
