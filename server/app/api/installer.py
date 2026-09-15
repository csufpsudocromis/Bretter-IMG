from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
import uuid

from ..core.security import get_current_user, create_agent_token
from ..core.database import get_db
from ..models.machine import Machine
from ..services.installer import build_installer_zip
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/installer", tags=["installer"])


class InstallerRequest(BaseModel):
    server_url: str
    machine_id: Optional[str] = None   # pre-assign a machine UUID (optional)
    label: Optional[str] = None        # friendly label shown in README


@router.post("/generate")
def generate_installer(
    body: InstallerRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Generate a Windows agent installer ZIP pre-configured with the given
    server URL and a fresh agent token. Returns the ZIP as a file download.
    """
    if not body.server_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="server_url must start with http:// or https://")

    # Use supplied machine_id or generate a new one
    machine_id = body.machine_id or str(uuid.uuid4())

    # Pre-register a placeholder machine record so the token is valid immediately
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        machine = Machine(
            id=machine_id,
            hostname=body.label or f"pending-{machine_id[:8]}",
        )
        db.add(machine)
        db.commit()

    # Issue an agent token for this machine
    agent_token = create_agent_token(machine_id)

    # Build the ZIP in memory
    zip_bytes = build_installer_zip(
        server_url=body.server_url,
        agent_token=agent_token,
        machine_id=machine_id,
    )

    filename = f"BretterIMG-Agent-{machine_id[:8]}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/bundle-status")
def bundle_status(_=Depends(get_current_user)):
    """Returns whether the Python bundle is available and its size."""
    from ..services.installer import bundle_available, BUNDLE_ZIP
    import os
    available = bundle_available()
    size_mb = round(os.path.getsize(BUNDLE_ZIP) / (1024 * 1024), 1) if available else 0
    return {"available": available, "size_mb": size_mb}


@router.get("/diagnose/{machine_id}")
def diagnose_agent(machine_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Returns diagnostic info for a machine: last seen, job history, token validity."""
    from ..models.machine import Machine
    from ..models.job import Job
    from ..core.security import create_agent_token
    from datetime import datetime, timedelta

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    jobs = db.query(Job).filter(Job.machine_id == machine_id).order_by(Job.created_at.desc()).limit(5).all()
    now = datetime.utcnow()
    last_seen_sec = (now - machine.last_seen).total_seconds() if machine.last_seen else None

    return {
        "machine_id": machine_id,
        "hostname": machine.hostname,
        "status": machine.status,
        "last_seen": machine.last_seen,
        "last_seen_seconds_ago": round(last_seen_sec) if last_seen_sec is not None else None,
        "ip_address": machine.ip_address,
        "os_version": machine.os_version,
        "agent_version": machine.agent_version,
        "recent_jobs": [{"id": j.id[:8], "type": j.type, "status": j.status, "log": j.log} for j in jobs],
        "tip": (
            "Agent is connecting normally" if machine.status == "online"
            else "Agent not yet connected — check C:\\ProgramData\\BretterIMG\\logs\\agent.log on the target machine"
        ),
    }
