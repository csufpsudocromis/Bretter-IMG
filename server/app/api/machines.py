from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import List
import uuid

from ..core.config import settings
from ..core.database import get_db
from ..core.security import create_agent_token, get_current_user, get_current_agent_machine_id
from ..models.image import Image
from ..models.job import Job, JobStatus, JobType
from ..models.machine import Machine, MachineStatus
from ..schemas.machine import MachineRegister, MachineHeartbeat, MachineUpdate, MachineOut, MachineRegisterOut
from ..services.agent_updates import CURRENT_AGENT_VERSION, agent_is_current, agent_version_is_current, ensure_agent_update_job
from ..services.post_deploy import ensure_post_deploy_job, recover_post_deploy_after_reboot

router = APIRouter(prefix="/api/machines", tags=["machines"])
OFFLINE_AFTER_SECONDS = max(60, settings.AGENT_POLL_INTERVAL * 4)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _machine_identity_values(machine: Machine) -> tuple[str, str]:
    mac = (machine.mac_address or "").strip().lower()
    hostname = (machine.hostname or "").strip().lower()
    return mac, hostname


def _find_same_mac_machine(db: Session, machine: Machine) -> Machine | None:
    mac, _ = _machine_identity_values(machine)
    if not mac:
        return None
    return (
        db.query(Machine)
        .filter(Machine.id != machine.id, Machine.mac_address.ilike(machine.mac_address.strip()))
        .order_by(Machine.last_seen.desc().nullslast())
        .first()
    )


def _carry_forward_duplicate_context(db: Session, machine: Machine) -> None:
    duplicate = _find_same_mac_machine(db, machine)
    if not duplicate:
        return
    if duplicate.group_id and not machine.group_id:
        machine.group_id = duplicate.group_id
    if duplicate.notes and not machine.notes:
        machine.notes = duplicate.notes
    duplicate.status = MachineStatus.offline


def _merge_same_mac_duplicate(db: Session, machine: Machine) -> None:
    duplicate = _find_same_mac_machine(db, machine)
    if not duplicate:
        return
    if duplicate.group_id and not machine.group_id:
        machine.group_id = duplicate.group_id
    if duplicate.notes and not machine.notes:
        machine.notes = duplicate.notes
    db.query(Job).filter(Job.machine_id == duplicate.id).update(
        {"machine_id": machine.id},
        synchronize_session=False,
    )
    db.query(Image).filter(Image.source_machine_id == duplicate.id).update(
        {"source_machine_id": machine.id},
        synchronize_session=False,
    )
    db.delete(duplicate)


def _refresh_stale_machine_status(machine: Machine, now: datetime | None = None) -> bool:
    if not machine.last_seen or machine.status not in (MachineStatus.online, MachineStatus.imaging):
        return False
    now = now or _utc_now_naive()
    if now - machine.last_seen <= timedelta(seconds=OFFLINE_AFTER_SECONDS):
        return False
    machine.status = MachineStatus.offline
    return True


def _refresh_stale_machines(db: Session, machines: list[Machine]) -> None:
    now = _utc_now_naive()
    changed = False
    for machine in machines:
        changed = _refresh_stale_machine_status(machine, now) or changed
    if changed:
        db.commit()


def _ensure_agent_updates_for_live_machines(db: Session, machines: list[Machine]) -> None:
    changed = False
    for machine in machines:
        if machine.status != MachineStatus.online:
            continue
        changed = bool(ensure_agent_update_job(db, machine)) or changed
    if changed:
        db.commit()


def _update_machine_from_register(machine: Machine, body: MachineRegister) -> None:
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "id":
            continue
        if field == "agent_version" and agent_is_current(machine):
            if not agent_version_is_current(value):
                continue
        setattr(machine, field, value)
    machine.last_seen = _utc_now_naive()
    machine.status = MachineStatus.online


def _has_machine_id_collision(machine: Machine, body: MachineRegister) -> bool:
    incoming_mac = (body.mac_address or "").strip().lower()
    existing_mac = (machine.mac_address or "").strip().lower()
    return bool(incoming_mac and existing_mac and incoming_mac != existing_mac)


def _ensure_collision_update_job(db: Session, existing: Machine, body: MachineRegister) -> None:
    if agent_version_is_current(body.agent_version):
        return
    active_statuses = [JobStatus.pending, JobStatus.sent, JobStatus.running]
    active = (
        db.query(Job)
        .filter(
            Job.machine_id == existing.id,
            Job.type == JobType.update_agent,
            Job.status.in_(active_statuses),
        )
        .first()
    )
    if active:
        return
    db.add(
        Job(
            id=str(uuid.uuid4()),
            type=JobType.update_agent,
            status=JobStatus.pending,
            machine_id=existing.id,
            capture_method="vss",
            sysprep_before_capture="false",
            created_by="system",
            log=(
                "Detected a reused/cloned machine ID from an older agent; "
                "queued compatibility update on the original ID.\n"
            ),
        )
    )


def _register_id_collision(db: Session, body: MachineRegister, existing: Machine) -> tuple[Machine, str]:
    """Handle cloned/reused installs where two hosts present the same agent ID."""
    _ensure_collision_update_job(db, existing, body)
    existing.status = MachineStatus.offline
    same_mac = (
        db.query(Machine)
        .filter(Machine.id != existing.id, Machine.mac_address.ilike(body.mac_address.strip()))
        .order_by(Machine.last_seen.desc().nullslast())
        .first()
        if body.mac_address
        else None
    )
    if same_mac:
        machine = same_mac
    else:
        data = body.model_dump()
        data["id"] = str(uuid.uuid4())
        machine = Machine(**data)
        db.add(machine)
        db.flush()
    _update_machine_from_register(machine, body)
    token = create_agent_token(machine.id)
    return machine, token


def _annotate_agent_update_status(machine: Machine) -> Machine:
    machine.current_agent_version = CURRENT_AGENT_VERSION
    machine.agent_update_available = not agent_is_current(machine)
    return machine


@router.post("/register", response_model=MachineRegisterOut)
def register_machine(body: MachineRegister, db: Session = Depends(get_db)):
    """Called by the agent on startup to register/update itself."""
    replacement_token = None
    machine = db.query(Machine).filter(Machine.id == body.id).first()
    if machine and _has_machine_id_collision(machine, body):
        machine, replacement_token = _register_id_collision(db, body, machine)
    elif machine:
        _update_machine_from_register(machine, body)
    else:
        machine = Machine(**body.model_dump(), status=MachineStatus.online, last_seen=_utc_now_naive())
        db.add(machine)
        db.flush()
    _carry_forward_duplicate_context(db, machine)
    _merge_same_mac_duplicate(db, machine)
    ensure_agent_update_job(db, machine)
    ensure_post_deploy_job(db, machine)
    recover_post_deploy_after_reboot(db, machine)
    db.commit()
    db.refresh(machine)
    result = MachineRegisterOut.model_validate(_annotate_agent_update_status(machine))
    result.agent_token = replacement_token
    return result


@router.post("/{machine_id}/heartbeat")
def heartbeat(
    machine_id: str,
    body: MachineHeartbeat,
    db: Session = Depends(get_db),
    agent_id: str = Depends(get_current_agent_machine_id),
):
    if agent_id != machine_id:
        raise HTTPException(status_code=403, detail="Token machine mismatch")
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    machine.last_seen = _utc_now_naive()
    machine.status = body.status
    db.commit()
    return {"ok": True}


@router.get("/", response_model=List[MachineOut])
def list_machines(db: Session = Depends(get_db), _=Depends(get_current_user)):
    machines = db.query(Machine).order_by(Machine.last_seen.desc().nullslast()).all()
    _refresh_stale_machines(db, machines)
    _ensure_agent_updates_for_live_machines(db, machines)
    for machine in machines:
        _annotate_agent_update_status(machine)
    return machines


@router.get("/{machine_id}", response_model=MachineOut)
def get_machine(machine_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if _refresh_stale_machine_status(machine):
        db.commit()
        db.refresh(machine)
    elif machine.status == MachineStatus.online:
        if ensure_agent_update_job(db, machine):
            db.commit()
    return _annotate_agent_update_status(machine)


@router.patch("/{machine_id}", response_model=MachineOut)
def update_machine(machine_id: str, body: MachineUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    for field, value in body.dict(exclude_unset=True).items():
        setattr(machine, field, value)
    db.commit()
    db.refresh(machine)
    return _annotate_agent_update_status(machine)


@router.delete("/{machine_id}")
def delete_machine(machine_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    db.delete(machine)
    db.commit()
    return {"ok": True}
