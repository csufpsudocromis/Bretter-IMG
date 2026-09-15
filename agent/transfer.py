"""
HTTP transport layer — communicates with the Bretter-IMG server.
Uses a short-lived token cache; re-registers if token expires.
"""

import requests
import os
import logging
from typing import Callable, Optional

from config import SERVER_URL, AGENT_TOKEN, TEMP_DIR

log = logging.getLogger("transfer")

# Shared session with auth header
_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {AGENT_TOKEN}"})


def _url(path: str) -> str:
    return f"{SERVER_URL.rstrip('/')}/{path.lstrip('/')}"


def register_machine(sysinfo: dict) -> dict:
    resp = requests.post(_url("/api/machines/register"), json=sysinfo, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_pending_jobs(machine_id: str) -> list:
    resp = _session.get(_url(f"/api/jobs/pending/{machine_id}"), timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_job_status(job_id: str, status: str, log_line: Optional[str] = None):
    payload = {"status": status}
    if log_line:
        payload["log"] = log_line
    try:
        resp = _session.patch(_url(f"/api/jobs/{job_id}"), json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to update job {job_id}: {e}")


def get_job_status(job_id: str) -> Optional[str]:
    try:
        resp = _session.get(_url(f"/api/jobs/{job_id}/agent-status"), timeout=5)
        resp.raise_for_status()
        return resp.json().get("status")
    except Exception as e:
        log.warning(f"Failed to fetch job {job_id} status: {e}")
        return None


def download_job_payload(
    job_id: str,
    dest_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    temp_path = f"{dest_path}.download"
    with _session.get(_url(f"/api/jobs/{job_id}/payload"), stream=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(temp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    pct = downloaded * 100 // total
                    progress_cb(f"Payload download: {pct}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)")
    os.replace(temp_path, dest_path)
    if progress_cb:
        progress_cb(f"Payload downloaded to {dest_path}")
    return dest_path


def upload_image(job_id: str, wim_path: str, name: str, machine_id: str):
    with open(wim_path, "rb") as f:
        resp = _session.post(
            _url("/api/images/agent-upload"),
            files={"file": (os.path.basename(wim_path), f, "application/octet-stream")},
            data={"name": name, "source_machine_id": machine_id, "job_id": job_id},
            timeout=None,
        )
    resp.raise_for_status()
    return resp.json()


def download_image(
    image_id: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    os.makedirs(TEMP_DIR, exist_ok=True)
    dest = os.path.join(TEMP_DIR, f"{image_id}.wim")

    with _session.get(_url(f"/api/images/{image_id}/download"), stream=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    pct = downloaded * 100 // total
                    progress_cb(f"Download: {pct}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)")

    if progress_cb:
        progress_cb(f"Download complete: {dest}")
    return dest
