"""
Bretter-IMG Agent — runs on Windows targets as a Windows service.

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
import json
import ctypes
import shutil
import zipfile

# Ensure bundled Python libs are on the path (important when run as a service)
_agent_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.normpath(os.path.join(_agent_dir, "..", "python"))
for _p in [
    _agent_dir,
    os.path.join(_python_dir, "Lib"),
    os.path.join(_python_dir, "Lib", "site-packages"),
    _python_dir,
]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from config import SERVER_URL, AGENT_ID_FILE, POLL_INTERVAL, TEMP_DIR
from sysinfo import collect_sysinfo

# ── Logging ──────────────────────────────────────────────────────────────────
log_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "BretterIMG", "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "agent.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("agent")
AGENT_RUNTIME_PATCH = "2026-09-08-machine-identity-rekey"

POST_DEPLOY_PENDING_FILE = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "BretterIMG", "post_deploy_pending.json"
)


class JobCancelled(Exception):
    """Raised when the console stops a running job."""


class PostDeployRebootPending(Exception):
    """Raised when post-deploy configuration must continue after a reboot."""


class WindowsSetupPending(Exception):
    """Raised when Windows first-boot setup/OOBE has not finished yet."""


def _error_message(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return f"{exc.__class__.__name__} raised without an error message"


def _current_windows_boot_epoch() -> float | None:
    if os.name != "nt":
        return None
    try:
        uptime_seconds = ctypes.windll.kernel32.GetTickCount64() / 1000
        return time.time() - uptime_seconds
    except Exception:
        return None


def _winpe_handoff_wait_message(state: dict) -> str | None:
    handoff_boot_epoch = state.get("handoff_boot_epoch")
    if not handoff_boot_epoch:
        return None
    current_boot_epoch = _current_windows_boot_epoch()
    if current_boot_epoch is None:
        return None
    try:
        handoff_boot_epoch = float(handoff_boot_epoch)
    except (TypeError, ValueError):
        return None
    if abs(current_boot_epoch - handoff_boot_epoch) > 30:
        return None

    handoff_created_at = state.get("handoff_created_at")
    elapsed = 0
    try:
        elapsed = int(time.time() - float(handoff_created_at))
    except (TypeError, ValueError):
        pass
    if elapsed > 900:
        return None
    return f"Waiting for scheduled WinPE reboot to start ({elapsed}s since handoff)"


# ── Import transfer AFTER logging is set up (it imports requests) ─────────────
try:
    from transfer import (
        register_machine,
        get_agent_token,
        set_agent_token,
        get_pending_jobs,
        update_job_status,
        get_job_status,
        upload_image,
        register_direct_capture,
        download_image,
        download_job_payload,
    )
except ImportError as e:
    log.critical(f"Failed to import transfer module: {e}")
    log.critical(f"sys.path = {sys.path}")
    sys.exit(1)

try:
    from imaging import (
        capture_image,
        deploy_image,
        run_wipe,
        winpe_capture_resume,
        winpe_capture_clear,
        winpe_capture_read_logs,
        WinPERebootPending,
        remove_winpe_environment,
        reset_next_boot_to_windows,
        _notify_client_popup,
    )
except ImportError as e:
    log.warning(f"Imaging module unavailable (DISM features disabled): {e}")
    capture_image = deploy_image = run_wipe = None
    winpe_capture_resume = winpe_capture_clear = winpe_capture_read_logs = WinPERebootPending = remove_winpe_environment = reset_next_boot_to_windows = _notify_client_popup = None


def get_or_create_machine_id() -> str:
    # Check for installer-seeded machine_id.txt in the agent directory
    # This ensures the agent uses the same UUID the server token was issued for
    seeded = os.path.join(_agent_dir, "machine_id.txt")
    if os.path.exists(seeded):
        with open(seeded) as f:
            mid = f.read().strip()
        if mid:
            os.makedirs(os.path.dirname(AGENT_ID_FILE), exist_ok=True)
            with open(AGENT_ID_FILE, "w") as f:
                f.write(mid)
            os.remove(seeded)
            log.info(f"Machine ID loaded from installer seed: {mid}")
            return mid

    os.makedirs(os.path.dirname(AGENT_ID_FILE), exist_ok=True)
    if os.path.exists(AGENT_ID_FILE):
        with open(AGENT_ID_FILE) as f:
            mid = f.read().strip()
            if mid:
                return mid
    machine_id = str(uuid.uuid4())
    with open(AGENT_ID_FILE, "w") as f:
        f.write(machine_id)
    log.info(f"New machine ID assigned: {machine_id}")
    return machine_id


def apply_registration_identity(current_machine_id: str, registration: dict) -> str:
    new_machine_id = str(registration.get("id") or "").strip()
    new_token = str(registration.get("agent_token") or "").strip()
    if new_machine_id and new_machine_id != current_machine_id:
        os.makedirs(os.path.dirname(AGENT_ID_FILE), exist_ok=True)
        with open(AGENT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(new_machine_id)
        log.warning("Server reassigned cloned/reused machine ID %s -> %s", current_machine_id, new_machine_id)
        current_machine_id = new_machine_id
    if new_token:
        set_agent_token(new_token)
        log.info("Stored reassigned agent token for machine ID %s", current_machine_id)
    return current_machine_id


def _trim_job_output(text: str, limit: int = 30000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n\n--- output truncated ---\n\n" + text[-limit // 2 :]


def _terminate_process_tree(process: subprocess.Popen):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
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


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _get_active_desktop_session_id() -> tuple:
    """
    Find the active interactive user session ID and username on Windows.
    Returns (session_id, username) or (None, "") if no active desktop session exists.
    """
    if os.name != "nt":
        return None, ""
    try:
        import ctypes.wintypes
        wtsapi32 = ctypes.windll.wtsapi32
        kernel32 = ctypes.windll.kernel32

        def _get_username(sess_id: int) -> str:
            try:
                p_buf = ctypes.c_wchar_p()
                returned = ctypes.wintypes.DWORD()
                # WTSUserName = 5
                if wtsapi32.WTSQuerySessionInformationW(0, sess_id, 5, ctypes.byref(p_buf), ctypes.byref(returned)):
                    user = p_buf.value or ""
                    wtsapi32.WTSFreeMemory(p_buf)
                    return user
            except Exception:
                pass
            return ""

        # 1. Try active console session first
        console_session = kernel32.WTSGetActiveConsoleSessionId()
        if console_session != 0xFFFFFFFF and console_session != 0:
            h_token = ctypes.wintypes.HANDLE()
            if wtsapi32.WTSQueryUserToken(console_session, ctypes.byref(h_token)):
                kernel32.CloseHandle(h_token)
                return console_session, _get_username(console_session)

        # 2. Enumerate sessions for active console or RDP sessions
        class WTS_SESSION_INFOW(ctypes.Structure):
            _fields_ = [
                ("SessionId", ctypes.wintypes.DWORD),
                ("pWinStationName", ctypes.wintypes.LPWSTR),
                ("State", ctypes.wintypes.DWORD),
            ]

        p_sessions = ctypes.POINTER(WTS_SESSION_INFOW)()
        count = ctypes.wintypes.DWORD()
        if wtsapi32.WTSEnumerateSessionsW(0, 0, 1, ctypes.byref(p_sessions), ctypes.byref(count)):
            try:
                for i in range(count.value):
                    sess = p_sessions[i]
                    # State == 0 is WTSActive
                    if sess.State == 0 and sess.SessionId != 0:
                        h_token = ctypes.wintypes.HANDLE()
                        if wtsapi32.WTSQueryUserToken(sess.SessionId, ctypes.byref(h_token)):
                            kernel32.CloseHandle(h_token)
                            return sess.SessionId, _get_username(sess.SessionId)
            finally:
                wtsapi32.WTSFreeMemory(p_sessions)
    except Exception as exc:
        log.debug("Session detection error: %s", exc)
    return None, ""


def _run_interactive_command_job(job: dict, progress):
    """
    Launches a command interactively directly into the active user's desktop session (winsta0\\default)
    breaking out of Windows Session 0 isolation so windows and GUI apps are visible.
    """
    shell = str(job.get("command_shell") or "").strip().lower()
    command = str(job.get("command_text") or "")
    timeout = int(job.get("command_timeout_seconds") or 300)

    if os.name != "nt":
        progress("Interactive execution is only supported on Windows; falling back to standard execution.")
        _run_headless_command_job(job, progress)
        return

    session_id, username = _get_active_desktop_session_id()
    if session_id is None:
        raise RuntimeError(
            "Cannot run command interactively: No active user is currently logged on to the desktop."
        )

    user_desc = f" (logged-in user: {username})" if username else ""
    progress(f"Targeting active desktop session {session_id}{user_desc}")

    if shell == "powershell":
        command_quoted = "$ProgressPreference = 'SilentlyContinue'; " + command
        encoded = base64.b64encode(command_quoted.encode("utf-16-le")).decode("ascii")
        cmdline = f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    elif shell == "cmd":
        cmdline = f"cmd.exe /c {command}"
    else:
        raise RuntimeError(f"Unsupported command shell: {shell or '(empty)'}")

    import ctypes.wintypes
    wtsapi32 = ctypes.windll.wtsapi32
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    userenv = ctypes.windll.userenv

    h_user_token = ctypes.wintypes.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(h_user_token)):
        err = kernel32.GetLastError()
        raise RuntimeError(f"Failed to query user token for session {session_id} (Windows error {err})")

    h_dup_token = ctypes.wintypes.HANDLE()
    TOKEN_ALL_ACCESS = 0xF01FF
    SECURITY_IMPERSONATION = 2
    TOKEN_PRIMARY = 1
    duplicated = advapi32.DuplicateTokenEx(
        h_user_token,
        TOKEN_ALL_ACCESS,
        None,
        SECURITY_IMPERSONATION,
        TOKEN_PRIMARY,
        ctypes.byref(h_dup_token),
    )
    token_to_use = h_dup_token if duplicated and h_dup_token.value else h_user_token

    env = ctypes.c_void_p()
    if not userenv.CreateEnvironmentBlock(ctypes.byref(env), token_to_use, False):
        env = None

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("lpReserved", ctypes.wintypes.LPWSTR),
            ("lpDesktop", ctypes.wintypes.LPWSTR),
            ("lpTitle", ctypes.wintypes.LPWSTR),
            ("dwX", ctypes.wintypes.DWORD),
            ("dwY", ctypes.wintypes.DWORD),
            ("dwXSize", ctypes.wintypes.DWORD),
            ("dwYSize", ctypes.wintypes.DWORD),
            ("dwXCountChars", ctypes.wintypes.DWORD),
            ("dwYCountChars", ctypes.wintypes.DWORD),
            ("dwFillAttribute", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("wShowWindow", ctypes.wintypes.WORD),
            ("cbReserved2", ctypes.wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", ctypes.wintypes.HANDLE),
            ("hStdOutput", ctypes.wintypes.HANDLE),
            ("hStdError", ctypes.wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.wintypes.HANDLE),
            ("hThread", ctypes.wintypes.HANDLE),
            ("dwProcessId", ctypes.wintypes.DWORD),
            ("dwThreadId", ctypes.wintypes.DWORD),
        ]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = "winsta0\\default"

    pi = PROCESS_INFORMATION()
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    NORMAL_PRIORITY_CLASS = 0x00000020
    CREATE_NEW_CONSOLE = 0x00000010
    creation_flags = CREATE_UNICODE_ENVIRONMENT | NORMAL_PRIORITY_CLASS | CREATE_NEW_CONSOLE

    progress(f"Spawning interactive {shell} command on session {session_id} desktop: {command}")

    cmd_buffer = ctypes.create_unicode_buffer(cmdline)
    success = advapi32.CreateProcessAsUserW(
        token_to_use,
        None,
        cmd_buffer,
        None,
        None,
        False,
        creation_flags,
        env,
        None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )

    if not success:
        err = kernel32.GetLastError()
        if env:
            userenv.DestroyEnvironmentBlock(env)
        if h_dup_token.value and h_dup_token.value != h_user_token.value:
            kernel32.CloseHandle(h_dup_token)
        kernel32.CloseHandle(h_user_token)
        raise RuntimeError(f"CreateProcessAsUserW failed with Windows error {err}")

    kernel32.CloseHandle(pi.hThread)
    started = time.monotonic()
    exit_code = ctypes.wintypes.DWORD()
    STILL_ACTIVE = 259

    try:
        while True:
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
            if exit_code.value != STILL_ACTIVE:
                break
            if time.monotonic() - started > timeout:
                kernel32.TerminateProcess(pi.hProcess, 1)
                raise RuntimeError(f"Interactive command timed out after {timeout}s")
            status = get_job_status(job["id"])
            if status == "cancelled":
                kernel32.TerminateProcess(pi.hProcess, 1)
                raise JobCancelled("Job was stopped from the console")
            time.sleep(1)

        progress(
            f"Interactive process (PID {pi.dwProcessId}) exited on session {session_id} desktop.\n"
            f"Exit code: {exit_code.value}"
        )
        if exit_code.value != 0:
            raise RuntimeError(f"Command exited with code {exit_code.value}")
    finally:
        kernel32.CloseHandle(pi.hProcess)
        if env:
            userenv.DestroyEnvironmentBlock(env)
        if h_dup_token.value and h_dup_token.value != h_user_token.value:
            kernel32.CloseHandle(h_dup_token)
        kernel32.CloseHandle(h_user_token)


def _run_headless_command_job(job: dict, progress):
    shell = str(job.get("command_shell") or "").strip().lower()
    command = str(job.get("command_text") or "")
    timeout = int(job.get("command_timeout_seconds") or 300)
    if shell == "powershell":
        command = "$ProgressPreference = 'SilentlyContinue'; " + command
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        args = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
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
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_file, open(
            stderr_path, "w", encoding="utf-8", errors="replace"
        ) as stderr_file:
            process = subprocess.Popen(args, stdout=stdout_file, stderr=stderr_file)
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    _terminate_process_tree(process)
                    stdout = _read_text_file(stdout_path)
                    stderr = _read_text_file(stderr_path)
                    output = _trim_job_output(
                        f"Command timed out after {timeout}s.\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}".strip()
                    )
                    if output:
                        progress(output)
                    raise RuntimeError(f"Command timed out after {timeout}s")
                status = get_job_status(job["id"])
                if status == "cancelled":
                    _terminate_process_tree(process)
                    raise JobCancelled("Job was stopped from the console")
                time.sleep(1)
        stdout = _read_text_file(stdout_path)
        stderr = _read_text_file(stderr_path)
        output = _trim_job_output(
            f"Exit code: {process.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}".strip()
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


def _run_remote_command_job(job: dict, progress):
    if _truthy(job.get("command_interactive")):
        _run_interactive_command_job(job, progress)
    else:
        _run_headless_command_job(job, progress)


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
    payload_name = job.get("transfer_payload_name") or "payload.zip"
    payload_dir = os.path.join(TEMP_DIR, "job-payloads")
    payload_path = os.path.join(payload_dir, f"{job['id']}-{os.path.basename(payload_name)}")
    progress(f"Downloading transfer payload for target directory: {target_path}")
    download_job_payload(job["id"], payload_path, progress_cb=progress)
    try:
        count = _safe_extract_zip(payload_path, target_path)
    finally:
        try:
            if os.path.exists(payload_path):
                os.remove(payload_path)
        except Exception:
            pass
    progress(f"Transferred {count} file(s) to {target_path}")


def handle_job(job: dict, machine_id: str):
    job_id = job["id"]
    job_type = job["type"]
    log.info(f"Executing job {job_id} type={job_type}")

    def progress(msg: str):
        log.info(msg)
        status = update_job_status(job_id, "running", log_line=msg)
        if status and status.get("status") == "cancelled":
            raise JobCancelled("Job was stopped from the console")

    try:
        update_job_status(job_id, "running")

        if job_type == "inventory":
            info = collect_sysinfo()
            progress(f"Inventory: {info.get('hostname')} / {info.get('os_version')}")
            register_machine({**info, "id": machine_id})

        elif job_type == "update_agent":
            progress("Checking for agent updates from server...")
            if reset_next_boot_to_windows:
                reset_next_boot_to_windows(progress)
            updated = self_update(restart=False, manual=True)
            if updated is None:
                progress("Agent update check could not reach the server; will retry automatically.")
            elif updated:
                progress(f"Updated {len(updated)} agent file(s): {', '.join(updated)}")
                progress("Agent update complete; restarting agent process...")
            else:
                progress("Agent files are already up to date; restarting agent process to reload version/build state.")

            update_job_status(job_id, "completed")
            log.info(f"Job {job_id} completed")
            restart_agent_process()
            return

        elif job_type == "capture":
            if not capture_image:
                raise RuntimeError("DISM imaging module not available")
            capture_name = job.get("capture_name") or f"capture-{machine_id[:8]}"
            capture_method = job.get("capture_method") or "vss"
            direct_capture_credentials = {
                "username": job.get("direct_capture_username") or "",
                "password": job.get("direct_capture_password") or "",
            }
            progress(f"Starting {capture_method.upper()} capture: {capture_name}")
            try:
                wim_path = capture_image(
                    capture_name,
                    method=capture_method,
                    job_id=job_id,
                    machine_id=machine_id,
                    direct_capture_credentials=direct_capture_credentials,
                    sysprep_before_capture=str(job.get("sysprep_before_capture", "")).lower() == "true",
                    unattended_file_path=job.get("unattended_file_path") or "",
                    progress_cb=progress,
                )
            except Exception as e:
                if WinPERebootPending and isinstance(e, WinPERebootPending):
                    # WinPE owns the operation now, so do not report completion
                    # before its offline work has actually run.
                    try:
                        progress(str(e))
                    except Exception as progress_exc:
                        log.info("Could not report WinPERebootPending to server (offline): %s", progress_exc)
                    return  # skip the update_job_status(completed) below
                raise
            progress(f"Capture complete, uploading {wim_path}")
            upload_image(job_id, wim_path, capture_name, machine_id)
            progress("Upload complete")
            os.remove(wim_path)

        elif job_type == "deploy":
            if not deploy_image:
                raise RuntimeError("DISM imaging module not available")
            if winpe_capture_clear:
                winpe_capture_clear()
            image_filename = job.get("deploy_image_filename")
            if not image_filename:
                raise RuntimeError("Deployment job is missing the selected image filename")
            progress("Staging WinPE restore from the configured SMB image share")
            try:
                deploy_image(
                    image_filename,
                    smb_credentials={
                        "username": job.get("smb_username") or "",
                        "password": job.get("smb_password") or "",
                    },
                    job_id=job_id,
                    machine_id=machine_id,
                    post_deploy={
                        "remove_winpe": job.get("remove_winpe_after_deploy", False),
                        "restore_hostname": job.get("restore_hostname_after_deploy", False),
                        "original_hostname": job.get("original_hostname"),
                        "domain_name": job.get("domain_name"),
                        "domain_username": job.get("domain_username"),
                        "domain_password": job.get("domain_password"),
                    },
                    progress_cb=progress,
                )
            except Exception as e:
                if WinPERebootPending and isinstance(e, WinPERebootPending):
                    progress(str(e))
                    return
                raise

        elif job_type == "push_winpe":
            from imaging import push_winpe_environment
            progress("Deploying WinPE boot environment to this machine...")
            push_winpe_environment(progress_cb=progress)
            progress("WinPE environment deployed. Capture and deploy jobs can now boot this machine into WinPE.")

        elif job_type == "post_deploy":
            progress("Applying post-deployment hostname/domain configuration...")
            try:
                _require_windows_setup_complete()
            except WindowsSetupPending as setup_pending:
                update_job_status(job_id, "pending", log_line=str(setup_pending))
                return
            post_deploy = {
                "remove_winpe": job.get("remove_winpe_after_deploy", False),
                "restore_hostname": job.get("restore_hostname_after_deploy", False),
                "original_hostname": job.get("original_hostname"),
                "domain_name": job.get("domain_name"),
                "domain_username": job.get("domain_username"),
                "domain_password": job.get("domain_password"),
            }
            try:
                post_messages = _apply_deploy_post_configuration(post_deploy, job_id=job_id)
            except PostDeployRebootPending as reboot:
                progress(str(reboot))
                return
            except WindowsSetupPending as setup_pending:
                update_job_status(job_id, "pending", log_line=str(setup_pending))
                return
            for message in post_messages:
                progress(message)
            if _truthy(job.get("restore_hostname_after_deploy")) or job.get("domain_name"):
                progress("Restarting to complete post-deployment configuration...")
                _restart_for_post_deploy("Bretter-IMG: Completing post-deployment configuration")

        elif job_type == "remote_command":
            _run_remote_command_job(job, progress)

        elif job_type == "file_transfer":
            _run_file_transfer_job(job, progress)

        elif job_type == "wipe":
            if not run_wipe:
                raise RuntimeError("Imaging module not available")
            progress("Starting secure wipe")
            run_wipe(progress_cb=progress)

        elif job_type == "reboot":
            progress("Rebooting system in 10 seconds")
            os.system("shutdown /r /t 10 /c \"Bretter-IMG: Scheduled reboot\"")

        elif job_type == "shutdown":
            progress("Shutting down system in 10 seconds")
            os.system("shutdown /s /t 10 /c \"Bretter-IMG: Scheduled shutdown\"")

        else:
            raise RuntimeError(f"Unsupported job type: {job_type}")

        update_job_status(job_id, "completed")
        log.info(f"Job {job_id} completed")

    except JobCancelled as e:
        log.warning(f"Job {job_id} stopped: {e}")
        update_job_status(job_id, "cancelled", log_line=str(e))

    except Exception as e:
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        err_msg = _error_message(e)
        if _notify_client_popup:
            try:
                _notify_client_popup(f"Bretter-IMG Job Failed ({job_type.upper()})", err_msg, is_error=True)
            except Exception:
                pass
        update_job_status(job_id, "failed", log_line=err_msg)


def check_winpe_resume(machine_id: str):
    """
    Called on agent startup. If a WinPE capture was in progress before reboot,
    wait for the WIM to appear (WinPE is still running DISM), then upload it.
    """
    state = _read_post_deploy_resume()
    if state:
        job_id = state.get("job_id")
        post_deploy = state.get("post_deploy") or {}
        previous_messages = state.get("messages") or []
        try:
            post_messages = _apply_deploy_post_configuration(post_deploy, job_id=job_id, stage="domain")
            _clear_post_deploy_resume()
            update_job_status(job_id, "completed", log_line="\n".join(previous_messages + post_messages))
            log.info("Post-deploy job %s completed successfully after hostname reboot", job_id)
            if post_deploy.get("domain_name"):
                _restart_for_post_deploy("Bretter-IMG: Completing domain join")
        except WindowsSetupPending as setup_pending:
            update_job_status(job_id, "pending", log_line=str(setup_pending))
        except Exception as e:
            message = "\n".join(previous_messages + [_error_message(e)])
            update_job_status(job_id, "failed", log_line=message)
            log.error("Post-deploy resume job %s failed: %s", job_id, message, exc_info=True)
        return

    if not winpe_capture_resume:
        return
    state = winpe_capture_resume()
    if not state:
        return

    if state.get("operation") == "deploy":
        job_id = state.get("job_id")
        result_paths = state.get("result_paths") or [state.get("result_path")]
        log_paths = (state.get("log_paths") or [state.get("log_path")]) + (state.get("dism_log_paths") or [])
        try:
            handoff_wait = _winpe_handoff_wait_message(state)
            if handoff_wait:
                status = update_job_status(job_id, "running", log_line=handoff_wait)
                if status and status.get("status") == "not_found":
                    winpe_capture_clear()
                    log.info("Cleared stale WinPE deployment resume state for missing job %s", job_id)
                return

            result = ""
            for result_path in result_paths:
                if result_path and os.path.exists(result_path):
                    with open(result_path, "r", encoding="utf-8", errors="replace") as f:
                        result = f.read().strip()
                    if result:
                        break
            logs = []
            for log_path in log_paths:
                if log_path and os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        logs.append(f"--- {os.path.basename(log_path)} ---\n{f.read()[-8000:]}")
            details = "\n".join(logs)
            if result.lower().startswith("completed:"):
                post_deploy = state.get("post_deploy") or {}
                winpe_capture_clear()
                if _post_deploy_requested(post_deploy):
                    update_job_status(
                        job_id,
                        "running",
                        log_line=f"{result}\nWaiting for automatic post-deploy tasks to complete.",
                    )
                    log.info("WinPE deployment job %s applied image and is waiting for automatic post-deploy", job_id)
                else:
                    update_job_status(job_id, "completed", log_line=result)
                    log.info("WinPE deployment job %s completed successfully", job_id)
            else:
                winpe_capture_clear()
                message = result or "WinPE deployment returned to Windows without a result file"
                if details:
                    message += "\n\nWinPE restore log:\n" + details
                update_job_status(job_id, "failed", log_line=message)
                log.error("WinPE deployment job %s failed: %s", job_id, message)
        except Exception as e:
            winpe_capture_clear()
            update_job_status(job_id, "failed", log_line=f"Could not read WinPE deployment result: {e}")
        return

    job_id   = state.get("job_id")
    name     = state.get("name", "capture")
    wim_path = state.get("wim_path")
    alternate_wim_paths = state.get("alternate_wim_paths") or []
    direct_capture = state.get("direct_capture") or {}
    wim_paths = []
    for path in [wim_path] + alternate_wim_paths:
        if path and path not in wim_paths:
            wim_paths.append(path)

    log.info(f"WinPE resume: pending capture job={job_id} wim paths={wim_paths}")

    def ensure_resume_is_active():
        status = get_job_status(job_id)
        if status is None:
            log.info(f"WinPE resume job {job_id} was not found; clearing stale pending resume state")
            winpe_capture_clear()
            return False
        if status in ("completed", "failed", "cancelled"):
            log.info(f"WinPE resume job is {status}; clearing pending resume state")
            winpe_capture_clear()
            return False
        return True

    def progress(msg):
        log.info(msg)
        status = update_job_status(job_id, "running", log_line=msg)
        if status and status.get("status") == "cancelled":
            winpe_capture_clear()
            raise JobCancelled("WinPE capture was stopped from the console")

    try:
        if not ensure_resume_is_active():
            return

        if direct_capture.get("enabled"):
            filename = direct_capture.get("filename")
            if not filename:
                raise RuntimeError("Direct WinPE capture did not save an output filename")
            progress("Checking server image store for direct WinPE capture output...")
            time.sleep(10)
            progress(f"Registering direct WinPE capture from server image store: {filename}")
            try:
                register_direct_capture(filename, name, job_id)
            except Exception as e:
                details = ""
                if winpe_capture_read_logs:
                    capture_log = winpe_capture_read_logs()
                    if capture_log:
                        details = "\n\nWinPE capture log:\n" + capture_log
                raise RuntimeError(
                    f"Direct WinPE capture output was not found in the server image store: {filename}. "
                    "Direct SMB capture does not use a local fallback; check the WinPE capture log above "
                    "for the DISM or SMB failure."
                    + details
                ) from e
            progress("Direct WinPE capture registered in Image Library")
            for candidate in wim_paths:
                try:
                    if candidate and os.path.exists(candidate):
                        os.remove(candidate)
                        progress(f"Removed local WinPE capture fallback file: {candidate}")
                except Exception as cleanup_error:
                    log.warning(f"Could not remove local WinPE fallback file {candidate}: {cleanup_error}")
            winpe_capture_clear()
            update_job_status(job_id, "completed")
            log.info(f"Direct WinPE capture job {job_id} completed successfully")
            return

        # Wait up to 2 hours for WinPE to finish capturing
        progress("Waiting for WinPE capture to complete...")
        waited = 0
        max_wait = 7200
        found_wim_path = None
        while True:
            for candidate in wim_paths:
                if os.path.exists(candidate):
                    found_wim_path = candidate
                    break
            if found_wim_path:
                break
            if waited >= max_wait:
                raise RuntimeError(f"WinPE capture timed out — WIM not found at: {', '.join(wim_paths)}")
            time.sleep(15)
            waited += 15
            if waited % 60 == 0:
                if not ensure_resume_is_active():
                    return
                progress(f"Still waiting for WinPE capture... ({waited}s elapsed)")

        progress(f"WIM found at {found_wim_path} — uploading...")
        upload_image(job_id, found_wim_path, name, machine_id)
        progress("Upload complete")
        os.remove(found_wim_path)
        winpe_capture_clear()
        update_job_status(job_id, "completed")
        log.info(f"WinPE capture job {job_id} completed successfully")

    except JobCancelled as e:
        log.warning(f"WinPE resume stopped: {e}")
        update_job_status(job_id, "cancelled", log_line=str(e))

    except Exception as e:
        log.error(f"WinPE resume failed: {e}", exc_info=True)
        winpe_capture_clear()
        update_job_status(job_id, "failed", log_line=_error_message(e))


def _powershell(script: str, timeout: int = 120) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "PowerShell command failed").strip())
    return (result.stdout or "").strip()


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _post_deploy_requested(post_deploy: dict) -> bool:
    return (
        _truthy(post_deploy.get("restore_hostname"))
        or bool(str(post_deploy.get("domain_name") or "").strip())
        or _truthy(post_deploy.get("remove_winpe"))
    )


def _registry_value(root, path: str, name: str):
    try:
        import winreg
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except Exception:
        return None


def _windows_setup_pending_reason(min_uptime_seconds: int = 300) -> str:
    if os.name != "nt":
        return ""
    try:
        uptime_seconds = int(ctypes.windll.kernel32.GetTickCount64() // 1000)
        if min_uptime_seconds > 0 and uptime_seconds < min_uptime_seconds:
            return f"first boot uptime is only {uptime_seconds}s"
    except Exception:
        pass
    try:
        import winreg
    except Exception:
        return ""

    setup_path = r"SYSTEM\Setup"
    checks = {
        "SystemSetupInProgress": _registry_value(winreg.HKEY_LOCAL_MACHINE, setup_path, "SystemSetupInProgress"),
        "OOBEInProgress": _registry_value(winreg.HKEY_LOCAL_MACHINE, setup_path, "OOBEInProgress"),
        "SetupType": _registry_value(winreg.HKEY_LOCAL_MACHINE, setup_path, "SetupType"),
    }
    pending = [f"{name}={value}" for name, value in checks.items() if value not in (None, 0, "0")]
    image_state = _registry_value(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State",
        "ImageState",
    )
    if image_state and str(image_state).upper() != "IMAGE_STATE_COMPLETE":
        pending.append(f"ImageState={image_state}")
    return ", ".join(pending)


def _require_windows_setup_complete(min_uptime_seconds: int = 300):
    reason = _windows_setup_pending_reason(min_uptime_seconds=min_uptime_seconds)
    if reason:
        raise WindowsSetupPending(f"Windows setup/OOBE is still in progress ({reason}); post-deploy will retry later")


def _domain_credential_username(domain_name: str, username: str) -> str:
    username = username.strip()
    if "\\" in username or "/" in username or "@" in username:
        return username
    return f"{username}@{domain_name}"


def _joined_domain_matches(domain_name: str) -> bool:
    if os.name != "nt" or not domain_name:
        return False
    script = f"""
$ErrorActionPreference = 'Stop'
$target = {_ps_quote(domain_name)}
$computerSystem = Get-CimInstance Win32_ComputerSystem
if ($computerSystem.PartOfDomain -and $computerSystem.Domain -ieq $target) {{
  'yes'
}}
"""
    try:
        return _powershell(script, timeout=30).strip().lower() == "yes"
    except Exception:
        return False


def _domain_readiness_pending_reason(domain_name: str) -> str:
    if os.name != "nt" or not domain_name:
        return ""
    script = f"""
$ErrorActionPreference = 'Continue'
$domain = {_ps_quote(domain_name)}
$problems = New-Object System.Collections.Generic.List[string]
try {{
  $service = Get-Service -Name Dhcp -ErrorAction SilentlyContinue
  if ($service -and $service.Status -ne 'Running') {{ $problems.Add("DHCP service is $($service.Status)") }}
}} catch {{ }}
try {{
  $dnsServers = Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object {{ $_.ServerAddresses -and $_.ServerAddresses.Count -gt 0 }} |
    Select-Object -ExpandProperty ServerAddresses -Unique
  if (-not $dnsServers) {{ $problems.Add('no IPv4 DNS servers are configured') }}
}} catch {{
  $problems.Add("could not read DNS server configuration: $($_.Exception.Message)")
}}
try {{
  Resolve-DnsName -Name "_ldap._tcp.dc._msdcs.$domain" -Type SRV -ErrorAction Stop | Out-Null
}} catch {{
  $problems.Add("domain controller SRV DNS lookup failed for $($domain): $($_.Exception.Message)")
}}
try {{
  $nltest = & nltest /dsgetdc:$domain 2>&1
  if ($LASTEXITCODE -ne 0) {{
    $problems.Add("domain controller discovery failed for $($domain): $($nltest -join ' ')")
  }}
}} catch {{
  $problems.Add("could not run nltest domain controller discovery: $($_.Exception.Message)")
}}
if ($problems.Count -gt 0) {{
  $problems -join '; '
}}
"""
    return _powershell(script, timeout=60).strip()


def _current_computer_name() -> str:
    try:
        result = subprocess.run(["hostname"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return os.environ.get("COMPUTERNAME", "").strip()


def _read_post_deploy_resume() -> dict:
    if not os.path.exists(POST_DEPLOY_PENDING_FILE):
        return {}
    try:
        with open(POST_DEPLOY_PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not read post-deploy pending state: {e}")
        return {}


def _clear_post_deploy_resume():
    if os.path.exists(POST_DEPLOY_PENDING_FILE):
        os.remove(POST_DEPLOY_PENDING_FILE)


def _write_post_deploy_resume(job_id: str, post_deploy: dict, messages: list[str]):
    os.makedirs(os.path.dirname(POST_DEPLOY_PENDING_FILE), exist_ok=True)
    state = {
        "operation": "post_deploy_resume",
        "job_id": job_id,
        "post_deploy": post_deploy,
        "messages": messages,
    }
    with open(POST_DEPLOY_PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _restart_for_post_deploy(comment: str):
    subprocess.Popen(["shutdown", "/r", "/t", "10", "/c", comment])


def _apply_deploy_post_configuration(
    post_deploy: dict,
    job_id: str = "",
    previous_messages=None,
    stage: str = "start",
) -> list[str]:
    min_uptime_seconds = 0 if stage == "domain" else 300
    _require_windows_setup_complete(min_uptime_seconds=min_uptime_seconds)
    messages = []
    previous_messages = previous_messages or []
    if stage == "start" and _truthy(post_deploy.get("remove_winpe")):
        if not remove_winpe_environment:
            raise RuntimeError("WinPE cleanup is unavailable in this agent version")
        remove_winpe_environment(lambda message: messages.append(message))

    hostname = ""
    if _truthy(post_deploy.get("restore_hostname")):
        hostname = str(post_deploy.get("original_hostname") or "").strip()
        if not hostname:
            raise RuntimeError("The original hostname was not recorded for this deployment")
    domain_name = str(post_deploy.get("domain_name") or "").strip()
    if domain_name:
        if hostname and stage != "domain" and _current_computer_name().lower() != hostname.lower():
            _powershell(f"Rename-Computer -NewName {_ps_quote(hostname)} -Force")
            next_state = dict(post_deploy)
            next_state["remove_winpe"] = False
            message = f"Restored hostname to {hostname}; restarting before joining domain {domain_name}"
            if not job_id:
                raise RuntimeError("Cannot continue post-deploy domain join after rename without a job id")
            _write_post_deploy_resume(job_id, next_state, previous_messages + messages + [message])
            _restart_for_post_deploy("Bretter-IMG: Completing hostname restore")
            raise PostDeployRebootPending(message)

        if _joined_domain_matches(domain_name):
            if hostname:
                messages.append(f"Already joined to domain {domain_name} after restoring hostname to {hostname}")
            else:
                messages.append(f"Already joined to domain {domain_name}")
            return previous_messages + messages

        username = str(post_deploy.get("domain_username") or "").strip()
        password = str(post_deploy.get("domain_password") or "")
        if not username or not password:
            raise RuntimeError("Domain join requires both a username and password")
        pending_reason = _domain_readiness_pending_reason(domain_name)
        if pending_reason:
            raise WindowsSetupPending(f"Domain {domain_name} is not reachable yet ({pending_reason}); post-deploy will retry later")
        credential_username = _domain_credential_username(domain_name, username)
        add_computer_args = f"-DomainName {_ps_quote(domain_name)} -Credential $credential -Force"
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$password = ConvertTo-SecureString {_ps_quote(password)} -AsPlainText -Force; "
            f"$credential = New-Object System.Management.Automation.PSCredential({_ps_quote(credential_username)}, $password); "
            f"Add-Computer {add_computer_args}"
        )
        _powershell(script, timeout=300)
        if hostname:
            messages.append(f"Joined domain {domain_name} after restoring hostname to {hostname}")
        else:
            messages.append(f"Joined domain {domain_name}")
    elif hostname:
        _powershell(f"Rename-Computer -NewName {_ps_quote(hostname)} -Force")
        messages.append(f"Restored hostname to {hostname}")
    return messages


def restart_agent_process():
    log.info("Self-update: restarting agent")
    time.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def self_update(restart: bool = True, manual: bool = False) -> list | None:
    """
    Check the server for updated agent files and replace any that have changed.
    Restarts the process if any files were updated and restart is True.
    """
    try:
        import hashlib, requests as _req
        from config import SERVER_URL
        try:
            from config import SERVER_CA_BUNDLE
        except ImportError:
            SERVER_CA_BUNDLE = ""
        headers = {"Authorization": f"Bearer {get_agent_token()}"}
        verify = SERVER_CA_BUNDLE if SERVER_CA_BUNDLE and os.path.exists(SERVER_CA_BUNDLE) else True
        manual_query = "?manual=1" if manual else ""
        manifest_url = f"{SERVER_URL.rstrip('/')}/api/agent/files/manifest{manual_query}"
        resp = _req.get(manifest_url, headers=headers, timeout=15, verify=verify)
        if resp.status_code != 200:
            log.warning(f"Self-update: manifest fetch failed ({resp.status_code})")
            return None
        manifest = resp.json()
        updated = []
        for fname, server_hash in manifest.items():
            local_path = os.path.join(_agent_dir, fname)
            if os.path.exists(local_path):
                with open(local_path, "rb") as f:
                    local_hash = hashlib.sha256(f.read()).hexdigest()
                if local_hash == server_hash:
                    continue  # already up to date
            # Download updated file
            file_url = f"{SERVER_URL.rstrip('/')}/api/agent/files/{fname}{manual_query}"
            r = _req.get(file_url, headers=headers, timeout=30, verify=verify)
            if r.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(r.content)
                log.info(f"Self-update: updated {fname}")
                updated.append(fname)
        if updated:
            log.info(f"Self-update: {len(updated)} file(s) updated")
            if restart:
                restart_agent_process()
        else:
            log.info("Self-update: agent is up to date")
        return updated
    except Exception as e:
        log.warning(f"Self-update check failed (non-fatal): {e}")
        return None


def main():
    log.info(f"=== Bretter-IMG Agent starting ===")
    log.info(f"Server: {SERVER_URL}")
    log.info(f"Python: {sys.executable} {sys.version}")
    log.info(f"Agent dir: {_agent_dir}")

    machine_id = get_or_create_machine_id()
    log.info(f"Machine ID: {machine_id}")

    # Register with retry loop (server may not be up yet)
    registered = False
    for attempt in range(1, 13):  # try for ~2 minutes
        try:
            sysinfo = collect_sysinfo()
            sysinfo["id"] = machine_id
            machine_id = apply_registration_identity(machine_id, register_machine(sysinfo))
            log.info(f"Registered with {SERVER_URL} as {sysinfo.get('hostname')}")
            registered = True
            break
        except Exception as e:
            log.warning(f"Registration attempt {attempt}/12 failed: {e}")
            time.sleep(10)

    if not registered:
        log.error("Could not register with server after 12 attempts. Continuing to poll anyway.")

    # Resume any WinPE capture/deploy that was interrupted by reboot
    check_winpe_resume(machine_id)

    log.info(f"Polling every {POLL_INTERVAL}s for jobs...")
    while True:
        try:
            sysinfo = collect_sysinfo()
            sysinfo["id"] = machine_id
            machine_id = apply_registration_identity(machine_id, register_machine(sysinfo))
            check_winpe_resume(machine_id)
            jobs = get_pending_jobs(machine_id)
            if jobs:
                log.info(f"Received {len(jobs)} job(s)")
            for job in jobs:
                handle_job(job, machine_id)
        except Exception as e:
            log.warning(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
