from datetime import datetime, timedelta
import uuid

from sqlalchemy.orm import Session

from ..models.job import Job, JobStatus, JobType
from ..models.machine import Machine

CURRENT_AGENT_BUILD = "2026-09-11-detailed-cpu"
CURRENT_AGENT_VERSION = "1.0.14"
STALE_SENT_UPDATE_DELAY = timedelta(minutes=2)
AUTO_UPDATE_RETRY_DELAY = timedelta(minutes=5)


def agent_is_current(machine: Machine) -> bool:
    return agent_version_is_current(machine.agent_version)


def agent_version_is_current(version: str | None) -> bool:
    found = _parse_agent_version(version)
    current = _parse_agent_version(CURRENT_AGENT_VERSION)
    if not found or not current:
        return False
    return found >= current


def _parse_agent_version(version: str | None) -> tuple[int, int, int] | None:
    version = (version or "").strip().split("+", 1)[0]
    parts = version.split(".")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


def agent_update_satisfied(db: Session, machine: Machine) -> bool:
    return agent_is_current(machine)


def ensure_agent_update_job(db: Session, machine: Machine) -> Job | None:
    if agent_update_satisfied(db, machine):
        return None

    active_statuses = [JobStatus.pending, JobStatus.sent, JobStatus.running]
    existing = (
        db.query(Job)
        .filter(
            Job.machine_id == machine.id,
            Job.type == JobType.update_agent,
            Job.status.in_(active_statuses),
        )
        .order_by(Job.created_at.desc())
        .first()
    )
    if existing:
        if (
            existing.status == JobStatus.sent
            and existing.started_at is None
            and existing.created_at
            and datetime.utcnow() - existing.created_at > STALE_SENT_UPDATE_DELAY
        ):
                existing.status = JobStatus.pending
                existing.log = (existing.log or "") + "Automatic update was sent but not started; retrying.\n"
        return existing

    latest_auto_attempt = (
        db.query(Job)
        .filter(
            Job.machine_id == machine.id,
            Job.type == JobType.update_agent,
            Job.status == JobStatus.completed,
            Job.created_by == "system",
        )
        .order_by(Job.completed_at.desc().nullslast(), Job.created_at.desc())
        .first()
    )
    if (
        latest_auto_attempt
        and latest_auto_attempt.completed_at
        and datetime.utcnow() - latest_auto_attempt.completed_at < AUTO_UPDATE_RETRY_DELAY
    ):
        return None

    job = Job(
        id=str(uuid.uuid4()),
        type=JobType.update_agent,
        status=JobStatus.pending,
        machine_id=machine.id,
        capture_method="vss",
        sysprep_before_capture="false",
        created_by="system",
        log=(
            f"Agent version {machine.agent_version or 'unknown'} is older than "
            f"{CURRENT_AGENT_VERSION}; queued automatic update.\n"
        ),
    )
    db.add(job)
    db.flush()
    return job
