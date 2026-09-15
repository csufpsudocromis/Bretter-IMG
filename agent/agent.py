"""
Bretter-IMG Agent — runs on Windows targets as a service.

Flow:
  1. On startup: collect system info, register with server, get agent token.
  2. Main loop: poll for pending jobs, execute them, report results.
"""

import time
import sys
import logging
import uuid
import os
import subprocess
import base64
import shutil
import zipfile

from config import SERVER_URL, AGENT_ID_FILE, POLL_INTERVAL, TEMP_DIR
from sysinfo import collect_sysinfo
from transfer import register_machine, get_pending_jobs, update_job_status, get_job_status, upload_image, download_image, download_job_payload
from imaging import capture_image, deploy_image, run_wipe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bretter-agent.log"),
    ],
)
log = logging.getLogger("agent")


class JobCancelled(Exception):
    """Raised when the console stops a running job."""


def get_or_create_machine_id() -> str:
    if os.path.exists(AGENT_ID_FILE):
        with open(AGENT_ID_FILE) as f:
            return f.read().strip()
    machine_id = str(uuid.uuid4())
    with open(AGENT_ID_FILE, "w") as f:
        f.write(machine_id)
    return machine_id


def _trim_job_output(text: str, limit: int = 30000) -> str:
    if len(text or "") <= limit:
        return text or ""
    return text[: limit // 2] + "\n\n--- output truncated ---\n\n" + text[-limit // 2 :]


def _terminate_process_tree(process: subprocess.Popen):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, timeout=15)
            return
        except Exception as exc:
            log.warning("taskkill failed for remote command pid %s: %s", process.pid, exc)
    try:
        process.kill()
    except Exception:
        pass


def _read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _run_remote_command_job(job: dict, progress):
    shell = str(job.get("command_shell") or "").strip().lower()
    command = str(job.get("command_text") or "")
    timeout = int(job.get("command_timeout_seconds") or 300)
    if shell == "powershell":
        command = "$ProgressPreference = 'SilentlyContinue'; " + command
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    elif shell == "cmd":
        args = ["cmd.exe", "/c", command]
    else:
        raise RuntimeError(f"Unsupported command shell: {shell or '(empty)'}")
    progress(f"Executing {shell} command with {timeout}s timeout")
    os.makedirs(TEMP_DIR, exist_ok=True)
    stdout_path = os.path.join(TEMP_DIR, f"{job['id']}-stdout.txt")
    stderr_path = os.path.join(TEMP_DIR, f"{job['id']}-stderr.txt")
    started = time.monotonic()
    process = None
    try:
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_file, open(stderr_path, "w", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(args, stdout=stdout_file, stderr=stderr_file)
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    _terminate_process_tree(process)
                    output = _trim_job_output(
                        f"Command timed out after {timeout}s.\n\nSTDOUT:\n{_read_text_file(stdout_path)}\n\nSTDERR:\n{_read_text_file(stderr_path)}".strip()
                    )
                    if output:
                        progress(output)
                    raise RuntimeError(f"Command timed out after {timeout}s")
                if get_job_status(job["id"]) == "cancelled":
                    _terminate_process_tree(process)
                    raise JobCancelled("Job was stopped from the console")
                time.sleep(1)
        output = _trim_job_output(
            f"Exit code: {process.returncode}\n\nSTDOUT:\n{_read_text_file(stdout_path)}\n\nSTDERR:\n{_read_text_file(stderr_path)}".strip()
        )
        if output:
            progress(output)
        if process.returncode != 0:
            raise RuntimeError(f"Command exited with code {process.returncode}")
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process)
        for path in (stdout_path, stderr_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def _safe_extract_zip(zip_path: str, target_dir: str) -> int:
    target_abs = os.path.abspath(target_dir)
    os.makedirs(target_abs, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_name = member.filename.replace("\\", "/")
            if not member_name or member_name.startswith("/") or member_name.startswith("../") or "/../" in member_name:
                raise RuntimeError(f"Unsafe archive entry rejected: {member.filename}")
            destination = os.path.abspath(os.path.join(target_abs, member_name))
            if os.path.commonpath([target_abs, destination]) != target_abs:
                raise RuntimeError(f"Unsafe archive entry rejected: {member.filename}")
            if member.is_dir():
                os.makedirs(destination, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as dest:
                shutil.copyfileobj(source, dest)
            extracted += 1
    return extracted


def _run_file_transfer_job(job: dict, progress):
    target_path = str(job.get("transfer_target_path") or "").strip()
    if not target_path:
        raise RuntimeError("File transfer job is missing the target directory")
    payload_dir = os.path.join(TEMP_DIR, "job-payloads")
    payload_path = os.path.join(payload_dir, f"{job['id']}-payload.zip")
    progress(f"Downloading transfer payload for target directory: {target_path}")
    download_job_payload(job["id"], payload_path, progress_cb=progress)
    try:
        count = _safe_extract_zip(payload_path, target_path)
    finally:
        if os.path.exists(payload_path):
            os.remove(payload_path)
    progress(f"Transferred {count} file(s) to {target_path}")


def handle_job(job: dict, machine_id: str):
    job_id = job["id"]
    job_type = job["type"]
    log.info(f"Executing job {job_id} type={job_type}")

    def progress(msg: str):
        log.info(msg)
        update_job_status(job_id, "running", log_line=msg)

    try:
        update_job_status(job_id, "running")

        if job_type == "inventory":
            info = collect_sysinfo()
            progress(f"Inventory complete: {info}")

        elif job_type == "capture":
            capture_name = job.get("capture_name") or "capture"
            wim_path = capture_image(capture_name, progress_cb=progress)
            progress(f"Capture complete, uploading {wim_path}")
            upload_image(job_id, wim_path, capture_name, machine_id)
            progress("Upload complete")
            os.remove(wim_path)

        elif job_type == "deploy":
            image_id = job["image_id"]
            progress(f"Downloading image {image_id}")
            wim_path = download_image(image_id, progress_cb=progress)
            progress("Download complete, applying image — system will reboot into WinPE")
            deploy_image(wim_path, progress_cb=progress)

        elif job_type == "wipe":
            progress("Starting secure wipe")
            run_wipe(progress_cb=progress)
            progress("Wipe complete")

        elif job_type == "remote_command":
            _run_remote_command_job(job, progress)

        elif job_type == "file_transfer":
            _run_file_transfer_job(job, progress)

        elif job_type == "reboot":
            progress("Rebooting system")
            os.system("shutdown /r /t 5")

        elif job_type == "shutdown":
            progress("Shutting down system")
            os.system("shutdown /s /t 5")

        else:
            raise RuntimeError(f"Unsupported job type: {job_type}")

        update_job_status(job_id, "completed")
        log.info(f"Job {job_id} completed successfully")

    except JobCancelled as e:
        log.warning(f"Job {job_id} stopped: {e}")
        update_job_status(job_id, "cancelled", log_line=str(e))

    except Exception as e:
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        update_job_status(job_id, "failed", log_line=str(e))


def main():
    machine_id = get_or_create_machine_id()
    log.info(f"Agent starting, machine_id={machine_id}")

    sysinfo = collect_sysinfo()
    sysinfo["id"] = machine_id
    register_machine(sysinfo)
    log.info("Registered with console. Polling for jobs...")

    while True:
        try:
            jobs = get_pending_jobs(machine_id)
            for job in jobs:
                handle_job(job, machine_id)
        except Exception as e:
            log.warning(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
