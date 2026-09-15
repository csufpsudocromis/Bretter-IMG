from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timezone
import uuid
import re
import json
import os
import shutil
import zipfile

from ..core.config import settings
from ..core.database import get_db
from ..core.security import get_current_user, get_current_agent_machine_id
from ..models.job import Job, JobStatus, JobType
from ..models.image import Image
from ..models.machine import Machine, MachineStatus
from ..schemas.job import AgentJobOut, JobCreate, JobOut, JobStatusUpdate
from ..services.agent_updates import agent_update_satisfied, ensure_agent_update_job
from ..services.samba import (
    SambaProvisionError,
    generate_smb_password,
    normalize_username,
    provision_samba_access,
)
from ..services.post_deploy import (
    complete_parent_deploy_from_post_deploy,
    ensure_post_deploy_for_deploy,
    ensure_post_deploy_job,
    recover_post_deploy_after_reboot,
)
from .winpe import _read_config

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
POST_DEPLOY_RETRY_DELAY_SECONDS = 30
COMMAND_SHELLS = {"cmd", "powershell"}
MAX_COMMAND_TIMEOUT_SECONDS = 24 * 60 * 60


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _clamp_progress_percent(value: float) -> int:
    return min(100, max(1, int(round(value))))


def _last_percent_from_text(log: str) -> int | None:
    matches = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", log)
    if not matches:
        return None
    return _clamp_progress_percent(float(matches[-1]))


def _extract_progress_percent(job: Job, log: str | None, status: JobStatus) -> int | None:
    if status == JobStatus.completed:
        return 100
    if not log:
        return None

    log_lower = log.lower()
    if job.type == JobType.deploy:
        apply_markers = (
            "winpe image apply progress",
            "image apply progress",
            "image applied successfully",
        )
        if not any(marker in log_lower for marker in apply_markers):
            return None
        if "image applied successfully" in log_lower:
            return 100
        return _last_percent_from_text(log)

    return _last_percent_from_text(log)


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


def _direct_capture_enabled() -> bool:
    config = _read_config()
    return (
        str(config.get("direct_capture_enabled", "")).strip().lower() in {"1", "true", "yes", "on"}
        and bool(config.get("direct_capture_share"))
    )


def _image_share_configured() -> bool:
    return bool(_read_config().get("direct_capture_share"))


def _ensure_user_share_access(user, db: Session):
    if user.smb_access_enabled and user.smb_username and user.smb_password:
        return
    user.smb_username = user.smb_username or normalize_username(user.username)
    user.smb_password = user.smb_password or generate_smb_password()
    try:
        provision_samba_access(user, user.smb_password)
    except (SambaProvisionError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not provision SMB access for {user.username}: {exc}")
    db.add(user)
    db.flush()


def _payload_dir(job_id: str) -> str:
    return os.path.join(settings.JOB_PAYLOAD_STORE_PATH, job_id)


def _payload_path(job_id: str, payload_name: str = "payload.zip") -> str:
    return os.path.join(_payload_dir(job_id), os.path.basename(payload_name))


def _validate_remote_command(body: JobCreate):
    shell = (body.command_shell or "").strip().lower()
    command = (body.command_text or "").strip()
    if shell not in COMMAND_SHELLS:
        raise HTTPException(status_code=400, detail="Command shell must be cmd or powershell")
    if not command:
        raise HTTPException(status_code=400, detail="Command text is required")
    timeout = body.command_timeout_seconds or 300
    if timeout < 1 or timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise HTTPException(status_code=400, detail="Command timeout must be between 1 second and 24 hours")


def _validate_transfer_target(target_path: str) -> str:
    target = (target_path or "").strip().strip('"')
    if not target:
        raise HTTPException(status_code=400, detail="Target directory is required")
    if len(target) > 500:
        raise HTTPException(status_code=400, detail="Target directory is too long")
    if "\x00" in target:
        raise HTTPException(status_code=400, detail="Target directory is invalid")
    return target


def _safe_archive_name(name: str, fallback: str) -> str:
    name = (name or fallback).replace("\\", "/").strip("/")
    parts = [part for part in name.split("/") if part and part not in (".", "..")]
    if not parts:
        parts = [fallback]
    return "/".join(parts)


def _copy_payload_for_job(source_payload: str, job_id: str) -> str:
    os.makedirs(_payload_dir(job_id), exist_ok=True)
    dest = _payload_path(job_id)
    shutil.copy2(source_payload, dest)
    return dest


@router.post("/", response_model=List[JobOut])
def create_jobs(body: JobCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    created = []
    use_creator_smb = (
        (body.type == JobType.capture and body.capture_method == "winpe" and _direct_capture_enabled())
        or (body.type == JobType.deploy and _image_share_configured())
    )
    if use_creator_smb:
        _ensure_user_share_access(user, db)
    image = None
    if body.type == JobType.deploy:
        if not body.image_id:
            raise HTTPException(status_code=400, detail="An image is required for deployment")
        if not _image_share_configured():
            raise HTTPException(
                status_code=400,
                detail="Configure the image SMB share in WinPE Assets before deploying an image",
            )
        image = db.query(Image).filter(Image.id == body.image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail=f"Image {body.image_id} not found")
        if bool(body.domain_name) != bool(body.domain_username) or bool(body.domain_name) != bool(body.domain_password):
            raise HTTPException(status_code=400, detail="Domain, username, and password are all required to join a domain")
    if body.type == JobType.capture and body.sysprep_before_capture and body.capture_method != "winpe":
        raise HTTPException(status_code=400, detail="Sysprep capture requires WinPE Offline Capture")
    if body.type == JobType.remote_command:
        _validate_remote_command(body)
    if body.type == JobType.file_transfer:
        raise HTTPException(status_code=400, detail="Use the file transfer endpoint for file transfer jobs")
    for machine_id in body.machine_ids:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if not machine:
            raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
        job = Job(
            id=str(uuid.uuid4()),
            type=body.type,
            machine_id=machine_id,
            image_id=body.image_id,
            capture_name=body.capture_name,
            capture_method=body.capture_method,
            scheduled_at=_normalize_utc_naive(body.scheduled_at),
            created_by=user.username,
            capture_smb_username=user.smb_username if use_creator_smb else None,
            capture_smb_password=user.smb_password if use_creator_smb else None,
            remove_winpe_after_deploy="true" if body.remove_winpe_after_deploy else "false",
            restore_hostname_after_deploy="true" if body.restore_hostname_after_deploy else "false",
            original_hostname=machine.hostname if body.type == JobType.deploy else None,
            domain_name=body.domain_name.strip() if body.domain_name else None,
            domain_username=body.domain_username.strip() if body.domain_username else None,
            domain_password=body.domain_password or None,
            sysprep_before_capture="true" if body.sysprep_before_capture else "false",
            unattended_file_path=body.unattended_file_path.strip() if body.unattended_file_path else None,
            command_shell=(body.command_shell or "").strip().lower() if body.command_shell else None,
            command_text=body.command_text,
            command_timeout_seconds=body.command_timeout_seconds,
            command_interactive="true" if body.command_interactive else "false",
        )
        db.add(job)
        created.append(job)
    db.commit()
    for j in created:
        db.refresh(j)
    return created


@router.post("/file-transfer", response_model=List[JobOut])
def create_file_transfer_jobs(
    machine_ids: str = Form(...),
    target_path: str = Form(...),
    file_paths: str = Form("[]"),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        parsed_machine_ids = json.loads(machine_ids)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="machine_ids must be a JSON list")
    if not isinstance(parsed_machine_ids, list) or not parsed_machine_ids:
        raise HTTPException(status_code=400, detail="At least one target machine is required")
    parsed_machine_ids = [str(machine_id) for machine_id in parsed_machine_ids if str(machine_id).strip()]
    if not parsed_machine_ids:
        raise HTTPException(status_code=400, detail="At least one target machine is required")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    try:
        parsed_file_paths = json.loads(file_paths)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="file_paths must be a JSON list")
    if not isinstance(parsed_file_paths, list):
        raise HTTPException(status_code=400, detail="file_paths must be a JSON list")
    target = _validate_transfer_target(target_path)

    machines = db.query(Machine).filter(Machine.id.in_(parsed_machine_ids)).all()
    if len(machines) != len(set(parsed_machine_ids)):
        raise HTTPException(status_code=404, detail="One or more machines were not found")

    os.makedirs(settings.JOB_PAYLOAD_STORE_PATH, exist_ok=True)
    temp_payload = os.path.join(settings.JOB_PAYLOAD_STORE_PATH, f"_upload-{uuid.uuid4()}.zip")
    try:
        with zipfile.ZipFile(temp_payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, upload in enumerate(files, start=1):
                requested_name = parsed_file_paths[index - 1] if index - 1 < len(parsed_file_paths) else upload.filename
                arcname = _safe_archive_name(str(requested_name or upload.filename or f"file-{index}"), f"file-{index}")
                with archive.open(arcname, "w") as dest:
                    shutil.copyfileobj(upload.file, dest)

        created = []
        for machine_id in parsed_machine_ids:
            job = Job(
                id=str(uuid.uuid4()),
                type=JobType.file_transfer,
                machine_id=machine_id,
                created_by=user.username,
                transfer_target_path=target,
                transfer_payload_name="payload.zip",
                transfer_extract_archive="true",
            )
            _copy_payload_for_job(temp_payload, job.id)
            db.add(job)
            created.append(job)
        db.commit()
        for job in created:
            db.refresh(job)
        return created
    finally:
        if os.path.exists(temp_payload):
            os.remove(temp_payload)


@router.get("/", response_model=List[JobOut])
def list_jobs(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Job).order_by(Job.created_at.desc()).all()


@router.get("/pending/{machine_id}", response_model=List[AgentJobOut])
def get_pending_jobs(
    machine_id: str,
    db: Session = Depends(get_db),
    agent_id: str = Depends(get_current_agent_machine_id),
):
    """Agents poll this to receive their next job."""
    if agent_id != machine_id:
        raise HTTPException(status_code=403, detail="Token machine mismatch")

    # Mark machine as online
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine:
        machine.last_seen = _utc_now_naive()
        machine.status = MachineStatus.online
        ensure_post_deploy_job(db, machine)
        recover_post_deploy_after_reboot(db, machine)
        ensure_agent_update_job(db, machine)

    # Older agents restart during self-update before they can mark the job complete.
    # If the agent has reconnected and is polling again, the update finished far
    # enough for the server to close those jobs instead of leaving the UI running.
    running_update_jobs = (
        db.query(Job)
        .filter(
            Job.machine_id == machine_id,
            Job.type == JobType.update_agent,
            Job.status == JobStatus.running,
        )
        .all()
    )
    for job in running_update_jobs:
        job.status = JobStatus.completed
        job.completed_at = _utc_now_naive()
        job.log = (job.log or "") + "Agent reconnected after update; marking update job complete.\n"

    if machine or running_update_jobs:
        db.commit()

    now = _utc_now_naive()
    query = (
        db.query(Job)
        .filter(
            Job.machine_id == machine_id,
            Job.status == JobStatus.pending,
            (Job.scheduled_at == None) | (Job.scheduled_at <= now),
        )
    )
    if machine and not agent_update_satisfied(db, machine):
        query = query.filter(Job.type == JobType.update_agent)
    jobs = query.order_by(Job.created_at).all()
    # Mark as sent
    for job in jobs:
        job.status = JobStatus.sent
    db.commit()
    agent_jobs = []
    for job in jobs:
        data = AgentJobOut.model_validate(job).model_dump()
        data["direct_capture_username"] = job.capture_smb_username
        data["direct_capture_password"] = job.capture_smb_password
        if job.type == JobType.deploy:
            image = db.query(Image).filter(Image.id == job.image_id).first()
            if not image:
                job.status = JobStatus.failed
                job.completed_at = _utc_now_naive()
                job.log = (job.log or "") + "Selected deployment image no longer exists.\n"
                continue
            data["deploy_image_filename"] = image.filename
            data["smb_username"] = job.capture_smb_username
            data["smb_password"] = job.capture_smb_password
        agent_jobs.append(data)
    db.commit()
    return agent_jobs


@router.delete("/history")
def clear_job_history(db: Session = Depends(get_db), _=Depends(get_current_user)):
    terminal_statuses = [JobStatus.completed, JobStatus.failed, JobStatus.cancelled]
    deleted = (
        db.query(Job)
        .filter(Job.status.in_(terminal_statuses))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "deleted": deleted}


@router.get("/{job_id}/agent-status")
def get_agent_job_status(
    job_id: str,
    db: Session = Depends(get_db),
    agent_id: str = Depends(get_current_agent_machine_id),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.machine_id != agent_id:
        raise HTTPException(status_code=403, detail="Not your job")
    return {"status": job.status}


@router.get("/{job_id}/payload")
def download_job_payload(
    job_id: str,
    db: Session = Depends(get_db),
    agent_id: str = Depends(get_current_agent_machine_id),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.machine_id != agent_id:
        raise HTTPException(status_code=403, detail="Not your job")
    if job.type != JobType.file_transfer:
        raise HTTPException(status_code=400, detail="Job has no file transfer payload")
    payload_name = job.transfer_payload_name or "payload.zip"
    path = _payload_path(job.id, payload_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Payload file not found")
    return FileResponse(path, media_type="application/zip", filename=payload_name)


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/{job_id}", response_model=JobOut)
def update_job_status(
    job_id: str,
    body: JobStatusUpdate,
    db: Session = Depends(get_db),
    agent_id: str = Depends(get_current_agent_machine_id),
):
    """Agents call this to report progress and completion."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.machine_id != agent_id:
        raise HTTPException(status_code=403, detail="Not your job")

    if job.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        if body.log:
            job.log = (job.log or "") + f"Ignored agent update after terminal status {job.status}: {body.log}\n"
        db.commit()
        db.refresh(job)
        return job

    job.status = body.status
    if body.log:
        job.log = (job.log or "") + body.log + "\n"
    progress_percent = _extract_progress_percent(job, body.log, body.status)
    if progress_percent is not None:
        job.progress_percent = progress_percent
    machine = db.query(Machine).filter(Machine.id == job.machine_id).first()
    if (
        job.type == JobType.deploy
        and job.status == JobStatus.completed
        and _deploy_requests_post_deploy(job)
    ):
        job.status = JobStatus.running
        job.completed_at = None
        job.log = (job.log or "") + "Image apply completed; waiting for automatic post-deploy tasks to complete.\n"
    if job.type == JobType.deploy and job.status in (JobStatus.running, JobStatus.completed) and machine:
        ensure_post_deploy_for_deploy(db, job, machine)
    if (
        job.type == JobType.post_deploy
        and body.status == JobStatus.pending
        and body.log
        and "post-deploy will retry later" in body.log.lower()
    ):
        from datetime import timedelta

        job.scheduled_at = _utc_now_naive() + timedelta(seconds=POST_DEPLOY_RETRY_DELAY_SECONDS)
    if body.status == JobStatus.running and not job.started_at:
        job.started_at = _utc_now_naive()
    if job.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        job.completed_at = _utc_now_naive()
        job.scheduled_at = None
        complete_parent_deploy_from_post_deploy(db, job)
        # Update machine status
        if machine:
            machine.status = MachineStatus.online if job.status in (JobStatus.completed, JobStatus.cancelled) else MachineStatus.error
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}")
def cancel_job(job_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.completed, JobStatus.failed):
        raise HTTPException(status_code=400, detail="Completed or failed jobs can only be removed with Clear History")
    if job.status == JobStatus.cancelled:
        return {"ok": True}

    old_status = job.status
    machine = db.query(Machine).filter(Machine.id == job.machine_id).first()
    if job.type == JobType.deploy and old_status == JobStatus.running and machine:
        child = ensure_post_deploy_for_deploy(db, job, machine)
        if child:
            job.log = (job.log or "") + (
                f"Stop requested by {user.username}, but the image is already applied; "
                f"leaving deploy active for automatic post-deploy job {child.id}.\n"
            )
            db.commit()
            return {"ok": True}

    job.status = JobStatus.cancelled
    job.completed_at = _utc_now_naive()
    action = "Stopped" if old_status == JobStatus.running else "Cancelled"
    job.log = (job.log or "") + f"{action} by {user.username}.\n"

    if machine and old_status == JobStatus.running:
        machine.status = MachineStatus.online

    db.commit()
    return {"ok": True}
