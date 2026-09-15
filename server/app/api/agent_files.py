"""
Serves agent source files so installed agents can self-update without
needing a full reinstall. The agent fetches its own source files on
startup and replaces them if the server has a newer version.
"""

import os
import hashlib
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse

from ..core.config import settings
from ..core.security import get_current_agent_machine_id

TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "agent_template")
)

UPDATABLE_FILES = ["agent.py", "imaging.py", "sysinfo.py", "transfer.py", "agent_build.txt"]

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


@router.get("/files/manifest")
def get_manifest(
    manual: bool = False,
    _agent_id: str = Depends(get_current_agent_machine_id),
):
    """Returns SHA-256 hashes of all updatable agent files."""
    if not settings.AGENT_AUTO_UPDATE_ENABLED and not manual:
        return {}
    manifest = {}
    for fname in UPDATABLE_FILES:
        fpath = os.path.join(TEMPLATE_DIR, fname)
        if os.path.exists(fpath):
            manifest[fname] = _file_hash(fpath)
    return manifest


@router.get("/files/{filename}")
def get_agent_file(
    filename: str,
    manual: bool = False,
    _agent_id: str = Depends(get_current_agent_machine_id),
):
    """Returns the latest version of an agent source file."""
    if not settings.AGENT_AUTO_UPDATE_ENABLED and not manual:
        raise HTTPException(status_code=404, detail="Automatic agent updates are disabled")
    if filename not in UPDATABLE_FILES:
        raise HTTPException(status_code=404, detail="File not available")
    fpath = os.path.join(TEMPLATE_DIR, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="File not found on server")
    return FileResponse(fpath, media_type="text/plain", filename=filename)
