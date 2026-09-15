"""
HTTP transport layer — communicates with the Bretter-IMG server.
Uses a short-lived token cache; re-registers if token expires.
"""

import requests
import os
import logging
import stat
import subprocess
import time
from typing import Callable, Optional

from config import SERVER_URL, AGENT_TOKEN, TEMP_DIR

try:
    from config import AGENT_TOKEN_FILE
except ImportError:
    AGENT_TOKEN_FILE = os.environ.get("BRETTER_AGENT_TOKEN_FILE", r"C:\ProgramData\BretterIMG\agent_token.txt")

try:
    from config import SERVER_CA_BUNDLE
except ImportError:
    SERVER_CA_BUNDLE = ""

log = logging.getLogger("transfer")

# Shared session with auth header
_session = requests.Session()


def _current_agent_token() -> str:
    try:
        if AGENT_TOKEN_FILE and os.path.exists(AGENT_TOKEN_FILE):
            with open(AGENT_TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    return token
    except Exception as exc:
        log.warning("Could not read persisted agent token: %s", exc)
    return AGENT_TOKEN


def get_agent_token() -> str:
    return _current_agent_token()


def set_agent_token(token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    _session.headers.update({"Authorization": f"Bearer {token}"})
    try:
        os.makedirs(os.path.dirname(AGENT_TOKEN_FILE), exist_ok=True)
        with open(AGENT_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception as exc:
        log.warning("Could not persist reassigned agent token: %s", exc)


_session.headers.update({"Authorization": f"Bearer {_current_agent_token()}"})
if SERVER_CA_BUNDLE and os.path.exists(SERVER_CA_BUNDLE):
    _session.verify = SERVER_CA_BUNDLE


def _url(path: str) -> str:
    return f"{SERVER_URL.rstrip('/')}/{path.lstrip('/')}"


def _clear_file_attributes(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["attrib", "-R", "-S", "-H", path],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass


def _replace_downloaded_file(temp_path: str, dest_path: str, progress_cb: Optional[Callable[[str], None]] = None) -> None:
    _clear_file_attributes(temp_path)
    if os.path.exists(dest_path):
        _clear_file_attributes(dest_path)

    errors = []
    for attempt in range(1, 6):
        try:
            os.replace(temp_path, dest_path)
            return
        except PermissionError as exc:
            errors.append(str(exc))
            if progress_cb and attempt == 1:
                progress_cb(
                    f"Existing {os.path.basename(dest_path)} is locked or protected; clearing attributes and retrying..."
                )
            if os.path.exists(dest_path):
                _clear_file_attributes(dest_path)
                try:
                    os.remove(dest_path)
                except Exception:
                    stale_path = f"{dest_path}.old"
                    if os.path.exists(stale_path):
                        _clear_file_attributes(stale_path)
                        try:
                            os.remove(stale_path)
                        except Exception:
                            pass
                    try:
                        os.replace(dest_path, stale_path)
                    except Exception:
                        pass
            time.sleep(attempt)
        except OSError as exc:
            errors.append(str(exc))
            time.sleep(attempt)

    if os.path.exists(temp_path):
        raise RuntimeError(
            f"Downloaded {os.path.basename(dest_path)}, but Windows would not replace {dest_path}. "
            "The existing file may still be mounted or locked by DISM. Reboot the machine or unmount any WinPE image, "
            f"then retry. Last error: {errors[-1] if errors else 'unknown'}"
        )


def register_machine(sysinfo: dict) -> dict:
    resp = _session.post(_url("/api/machines/register"), json=sysinfo, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_pending_jobs(machine_id: str) -> list:
    resp = _session.get(_url(f"/api/jobs/pending/{machine_id}"), timeout=5)
    resp.raise_for_status()
    return resp.json()


def update_job_status(job_id: str, status: str, log_line: Optional[str] = None):
    payload = {"status": status}
    if log_line:
        payload["log"] = log_line
    try:
        resp = _session.patch(_url(f"/api/jobs/{job_id}"), json=payload, timeout=5)
        if resp.status_code == 404:
            return {"status": "not_found"}
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Failed to update job {job_id}: {e}")
        return None


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
                    progress_cb(
                        f"Payload download: {pct}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)"
                    )
    os.replace(temp_path, dest_path)
    if progress_cb:
        progress_cb(f"Payload downloaded to {dest_path}")
    return dest_path


def get_winpe_config() -> dict:
    try:
        resp = _session.get(_url("/api/winpe/config/agent"), timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Failed to fetch WinPE config: {e}")
        return {}


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


def register_direct_capture(filename: str, name: str, job_id: str = ""):
    resp = _session.post(
        _url("/api/images/agent-register-direct"),
        json={"filename": filename, "name": name, "job_id": job_id},
        timeout=300,
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


def download_winpe_asset(
    filename: str,
    dest_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    temp_path = f"{dest_path}.download"

    with _session.get(_url(f"/api/winpe/assets/{filename}"), stream=True, timeout=None) as r:
        if r.status_code == 404:
            hint = ""
            if filename in ("boot.wim", "boot.sdi"):
                hint = " Upload boot.wim and boot.sdi in the console first."
            raise RuntimeError(
                f"Server is missing WinPE asset {filename}.{hint}"
            )
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
                    progress_cb(
                        f"{filename}: {pct}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)"
                    )

    _replace_downloaded_file(temp_path, dest_path, progress_cb)
    if progress_cb:
        progress_cb(f"Copied {filename} to {dest_path}")
    return dest_path


def get_winpe_drivers() -> list:
    try:
        resp = _session.get(_url("/api/winpe/drivers/agent"), timeout=15)
        resp.raise_for_status()
        return resp.json().get("drivers", [])
    except Exception as e:
        log.warning(f"Failed to fetch WinPE driver manifest: {e}")
        return []


def download_winpe_driver(
    filename: str,
    dest_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    temp_path = f"{dest_path}.download"

    with _session.get(_url(f"/api/winpe/drivers/{filename}"), stream=True, timeout=None) as r:
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
                    progress_cb(
                        f"{filename}: {pct}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)"
                    )

    os.replace(temp_path, dest_path)
    if progress_cb:
        progress_cb(f"Copied WinPE driver {filename} to {dest_path}")
    return dest_path
