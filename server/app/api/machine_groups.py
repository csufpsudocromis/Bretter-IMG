from datetime import datetime
from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.security import get_current_user
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..schemas.machine_group import (
    MachineGroupAssign,
    MachineGroupCreate,
    MachineGroupOut,
    MachineGroupUpdate,
)

router = APIRouter(prefix="/api/machine-groups", tags=["machine-groups"])


def _unique_machine_count(machines: list[Machine]) -> int:
    return len(machines or [])


def _group_out(group: MachineGroup) -> MachineGroupOut:
    return MachineGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        created_at=group.created_at,
        machine_count=_unique_machine_count(group.machines or []),
    )


def _ensure_unique_name(db: Session, name: str, group_id: str | None = None):
    existing = db.query(MachineGroup).filter(MachineGroup.name == name).first()
    if existing and existing.id != group_id:
        raise HTTPException(status_code=400, detail="A machine group with that name already exists")


@router.get("/", response_model=List[MachineGroupOut])
def list_machine_groups(db: Session = Depends(get_db), _=Depends(get_current_user)):
    groups = db.query(MachineGroup).order_by(MachineGroup.name).all()
    return [_group_out(group) for group in groups]


@router.post("/", response_model=MachineGroupOut)
def create_machine_group(body: MachineGroupCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    _ensure_unique_name(db, name)
    group = MachineGroup(
        id=str(uuid.uuid4()),
        name=name,
        description=(body.description or "").strip() or None,
        created_at=datetime.utcnow(),
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return _group_out(group)


@router.patch("/{group_id}", response_model=MachineGroupOut)
def update_machine_group(
    group_id: str,
    body: MachineGroupUpdate,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    group = db.query(MachineGroup).filter(MachineGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Machine group not found")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Group name is required")
        _ensure_unique_name(db, name, group_id=group.id)
        group.name = name
    if body.description is not None:
        group.description = body.description.strip() or None
    db.commit()
    db.refresh(group)
    return _group_out(group)


@router.post("/{group_id}/machines", response_model=MachineGroupOut)
def assign_machines_to_group(
    group_id: str,
    body: MachineGroupAssign,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    group = db.query(MachineGroup).filter(MachineGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Machine group not found")
    machines = db.query(Machine).filter(Machine.id.in_(body.machine_ids)).all()
    if len(machines) != len(set(body.machine_ids)):
        raise HTTPException(status_code=404, detail="One or more machines were not found")
    for machine in machines:
        machine.group_id = group.id
    db.commit()
    db.refresh(group)
    return _group_out(group)


@router.post("/ungroup", response_model=dict)
def ungroup_machines(body: MachineGroupAssign, db: Session = Depends(get_db), _=Depends(get_current_user)):
    machines = db.query(Machine).filter(Machine.id.in_(body.machine_ids)).all()
    if len(machines) != len(set(body.machine_ids)):
        raise HTTPException(status_code=404, detail="One or more machines were not found")
    for machine in machines:
        machine.group_id = None
    db.commit()
    return {"ok": True, "updated": len(machines)}


@router.delete("/{group_id}")
def delete_machine_group(group_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    group = db.query(MachineGroup).filter(MachineGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Machine group not found")
    for machine in group.machines:
        machine.group_id = None
    db.delete(group)
    db.commit()
    return {"ok": True}
