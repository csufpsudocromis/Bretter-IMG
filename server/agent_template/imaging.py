"""
Imaging operations using Windows DISM, VSS, and WinPE.

Capture:  Uses VSS (Volume Shadow Copy) to snapshot the live C: drive,
          then DISM captures from the shadow copy — no reboot required.
          Falls back to a WinPE-based offline capture if VSS fails.

Deploy:   Writes a WinPE auto-restore script and reboots into WinPE,
          which applies the WIM offline then reboots into the new OS.

Wipe:     Overwrites the drive with zeros using diskpart clean all.
"""

import subprocess
import os
import time
import logging
import tempfile
import re
import shutil
import base64
import json
import ctypes
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from config import AGENT_TOKEN, SERVER_URL, TEMP_DIR, WINPE_STAGE_DIR

try:
    from config import AGENT_TOKEN_FILE
except ImportError:
    AGENT_TOKEN_FILE = os.environ.get("BRETTER_AGENT_TOKEN_FILE", r"C:\ProgramData\BretterIMG\agent_token.txt")

try:
    from config import WINPE_BOOT_ID, WINPE_BOOT_DESCRIPTION
except ImportError:
    WINPE_BOOT_ID = os.environ.get("BRETTER_WINPE_BOOT_ID", "")
    WINPE_BOOT_DESCRIPTION = os.environ.get("BRETTER_WINPE_BOOT_DESCRIPTION", "Bretter-IMG WinPE")

log = logging.getLogger("imaging")
IMAGING_RUNTIME_PATCH = "2026-09-08-machine-identity-rekey"

VSS_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vss_helper.ps1")
WINPE_BOOT_WIM = os.path.join(WINPE_STAGE_DIR, "boot.wim")
WINPE_BOOT_SDI = os.path.join(WINPE_STAGE_DIR, "boot.sdi")
WINPE_BOOT_ENTRY = os.path.join(WINPE_STAGE_DIR, "winpe-entry.cmd")
WINPE_DRIVER_DIR = os.path.join(WINPE_STAGE_DIR, "drivers")
WINPE_BACKGROUND_IMAGE = os.path.join(WINPE_STAGE_DIR, "background.jpg")
WINPE_ACTIVE_WIM_FILE = os.path.join(WINPE_STAGE_DIR, "active-boot-wim.txt")


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


def _system_drive() -> str:
    drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
    return drive if drive.endswith(":") else "C:"


def _system_cmd(exe_name: str) -> str:
    if os.name != "nt":
        return exe_name
    windir = os.environ.get("WINDIR", r"C:\Windows")
    if os.path.splitext(exe_name)[1]:
        names = [exe_name]
    else:
        names = [f"{exe_name}.exe", exe_name]
    for name in names:
        sysnative = os.path.join(windir, "sysnative", name)
        if os.path.isfile(sysnative):
            return sysnative
        system32 = os.path.join(windir, "System32", name)
        if os.path.isfile(system32):
            return system32
    return exe_name


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _safe_capture_filename(name: str, ext: str = ".wim") -> str:
    stem = (name or "").strip()
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = "image"

    ext = ext if ext.startswith(".") else f".{ext}"
    if not ext or len(ext) > 10:
        ext = ".wim"

    max_stem = max(1, 180 - len(ext))
    return f"{stem[:max_stem].rstrip(' .')}{ext}"


def _notify_client_popup(title: str, message: str, is_error: bool = False):
    """
    Display a native GUI popup message box on client desktops (Session 0 -> Active Console Session)
    and log to local status files.
    """
    log.info("Client screen popup [%s]: %s", title, message)
    for status_path in (
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "BretterIMG", "agent-notifications.txt"),
        os.path.join(_system_drive() + "\\", "BretterIMG-Status.txt"),
    ):
        try:
            os.makedirs(os.path.dirname(status_path), exist_ok=True)
            with open(status_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{title}] {message}\n")
        except Exception:
            pass

    if os.name == "nt":
        # 1. Native WTSSendMessage via C# in PowerShell for Session 0 -> Active Desktop User
        try:
            style = 0x00000010 if is_error else 0x00000040  # MB_ICONERROR vs MB_ICONINFORMATION
            ps_script = f"""
$code = @'
using System;
using System.Runtime.InteropServices;
public class BretterPopup {{
    [DllImport("wtsapi32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern bool WTSSendMessage(
        IntPtr hServer,
        int SessionId,
        string pTitle,
        int TitleLength,
        string pMessage,
        int MessageLength,
        int Style,
        int Timeout,
        out int pResponse,
        bool bWait);

    [DllImport("kernel32.dll")]
    public static extern uint WTSGetActiveConsoleSessionId();

    public static void Show(string title, string msg, int style) {{
        int response;
        uint sessionId = WTSGetActiveConsoleSessionId();
        if (sessionId != 0xFFFFFFFF) {{
            WTSSendMessage(IntPtr.Zero, (int)sessionId, title, title.Length * 2, msg, msg.Length * 2, style, 30, out response, false);
        }}
    }}
}}
'@
Add-Type -TypeDefinition $code -ErrorAction SilentlyContinue
[BretterPopup]::Show({_ps_quote(title)}, {_ps_quote(message)}, {style})
"""
            encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
            powershell_exe = _system_cmd("powershell.exe")
            subprocess.Popen([
                powershell_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded
            ])
        except Exception as exc:
            log.debug("WTSSendMessage popup error: %s", exc)

        # 2. msg.exe fallback
        try:
            msg_exe = _system_cmd("msg.exe")
            if os.path.exists(msg_exe):
                subprocess.Popen([msg_exe, "*", "/TIME:30", f"{title}\n\n{message}"])
        except Exception as exc:
            log.debug("msg.exe popup error: %s", exc)


def _notify_client_screen(message: str):
    _notify_client_popup("Bretter-IMG Status", message, is_error=False)


def _run(cmd: list, progress_cb: Optional[Callable] = None, timeout: int = 7200) -> str:
    full_cmd = list(cmd)
    if full_cmd and isinstance(full_cmd[0], str) and os.name == "nt":
        full_cmd[0] = _system_cmd(full_cmd[0])
    log.info("Running: %s", " ".join(str(c) for c in full_cmd))
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start command {' '.join(str(c) for c in full_cmd)}: {exc}") from exc
    if progress_cb and result.stdout:
        for line in result.stdout.splitlines():
            if line.strip():
                _safe_progress(progress_cb, line)
    if result.returncode != 0:
        out = (result.stdout + result.stderr)[:1000]
        raise RuntimeError(f"Command failed (rc={result.returncode}): {out}")
    return result.stdout


def _parse_bcd_entries(output: str) -> list:
    entries = []
    current = {}
    known_fields = {
        "identifier", "device", "path", "description", "locale", "inherit",
        "default", "resumeobject", "displayorder", "toolsdisplayorder",
        "timeout", "osdevice", "systemroot", "nx", "pae", "winpe",
        "detecthal", "bootmenupolicy", "isolatedcontext", "allowedinmemorysettings",
    }

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or set(line) == {"-"}:
            continue

        parts = line.split(None, 1)
        key = parts[0].lower()
        if len(parts) == 2 and key in known_fields:
            current[key] = parts[1].strip()
            continue

        if current:
            entries.append(current)
        current = {"section": line}

    if current:
        entries.append(current)
    return entries


def _find_winpe_boot_id_optional(progress_cb: Optional[Callable] = None) -> Optional[str]:
    configured = WINPE_BOOT_ID.strip()
    if configured:
        return configured

    output = _run(["bcdedit", "/enum", "all"], timeout=60)
    desired_descriptions = [desc.lower() for desc in _winpe_boot_descriptions()]
    candidates = []
    for entry in _parse_bcd_entries(output):
        identifier = entry.get("identifier", "")
        description = entry.get("description", "")
        is_winpe = entry.get("winpe", "").lower() == "yes"
        description_lower = description.lower()
        if identifier and any(desired in description_lower for desired in desired_descriptions):
            candidates.append((0, identifier, description))
        elif identifier and is_winpe and "winpe" in description_lower:
            candidates.append((1, identifier, description))
        elif identifier and "winpe" in description_lower:
            candidates.append((2, identifier, description))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        boot_id = candidates[0][1]
        if progress_cb:
            progress_cb(f"Using WinPE boot entry {boot_id} ({candidates[0][2]})")
        return boot_id

    return None


def _find_winpe_boot_id(progress_cb: Optional[Callable] = None) -> str:
    boot_id = _find_winpe_boot_id_optional(progress_cb)
    if boot_id:
        return boot_id

    raise RuntimeError(
        "No WinPE BCD boot entry was found. Run Push WinPE on this machine first, "
        f"or set BRETTER_WINPE_BOOT_ID to the entry identifier shown by 'bcdedit /enum all'."
    )


def _schedule_winpe_reboot(
    reason: str,
    delay_seconds: int,
    progress_cb: Optional[Callable] = None,
    boot_id: Optional[str] = None,
    screen_message: Optional[str] = None,
):
    if not boot_id:
        boot_id = _find_winpe_boot_id(progress_cb)
    _safe_progress(progress_cb, f"Scheduling one-time boot to WinPE entry {boot_id}")
    _run(["bcdedit", "/bootsequence", boot_id], timeout=60)
    _safe_progress(progress_cb, "One-time WinPE boot sequence set; rebooting now")
    _notify_client_screen(screen_message or "Bretter-IMG: Rebooting into WinPE now...")
    if os.name == "nt":
        shutdown_exe = _system_cmd("shutdown.exe")
        log.info("Executing shutdown command: %s /r /f /t %s /c %s", shutdown_exe, delay_seconds, reason)
        subprocess.Popen([shutdown_exe, "/r", "/f", "/t", str(delay_seconds), "/c", reason])
    else:
        subprocess.Popen(["shutdown", "-r", f"+{delay_seconds}", reason])


def _prepare_winpe_boot(progress_cb: Optional[Callable] = None):
    boot_id = _find_winpe_boot_id(progress_cb)
    _safe_progress(progress_cb, f"Scheduling one-time boot to WinPE entry {boot_id}")
    _run(["bcdedit", "/bootsequence", boot_id], timeout=60)


def reset_next_boot_to_windows(progress_cb: Optional[Callable] = None):
    """Point the one-time boot sequence back at the running Windows install."""
    if os.name != "nt":
        return
    try:
        _run(["bcdedit", "/bootsequence", "{current}"], timeout=60)
        if progress_cb:
            try:
                progress_cb("Next boot reset to the current Windows OS")
            except Exception as progress_exc:
                log.info("Could not report next-boot reset progress: %s", progress_exc)
    except Exception as exc:
        log.warning("Could not reset one-time boot sequence to Windows: %s", exc)
        if progress_cb:
            try:
                progress_cb(f"Could not reset one-time boot sequence to Windows: {exc}")
            except Exception as progress_exc:
                log.info("Could not report next-boot reset failure: %s", progress_exc)


def _safe_progress(progress_cb: Optional[Callable], msg: str):
    if not progress_cb:
        return
    try:
        progress_cb(msg)
    except Exception as exc:
        log.info("Progress callback offline: %s", exc)


def _sysprep_log_excerpt(since_timestamp: Optional[float] = None) -> str:
    sysprep_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "Sysprep", "Panther")
    panther_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Panther")
    excerpts = []
    # Primary: check setuperr.log (actual error log)
    for log_dir in (sysprep_dir, panther_dir):
        err_path = os.path.join(log_dir, "setuperr.log")
        if os.path.exists(err_path):
            if since_timestamp is not None:
                try:
                    if os.path.getmtime(err_path) < since_timestamp - 5:
                        continue
                except OSError:
                    continue
            try:
                with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                if content:
                    excerpts.append(f"--- {err_path} ---\n{content[-4000:]}")
            except OSError:
                pass
    if not excerpts:
        # Fall back to setupact.log if setuperr.log is empty or missing
        for log_dir in (sysprep_dir, panther_dir):
            act_path = os.path.join(log_dir, "setupact.log")
            if os.path.exists(act_path):
                if since_timestamp is not None:
                    try:
                        if os.path.getmtime(act_path) < since_timestamp - 5:
                            continue
                    except OSError:
                        continue
                try:
                    with open(act_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read().strip()
                    if content:
                        excerpts.append(f"--- {act_path} ---\n{content[-4000:]}")
                except OSError:
                    pass
    return "\n".join(excerpts)


def _sysprep_status() -> str:
    status_str = ""
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\Setup\Status\SysprepStatus", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            gen, _ = winreg.QueryValueEx(key, "GeneralizationState")
            clean, _ = winreg.QueryValueEx(key, "CleanupState")
            winreg.CloseKey(key)
            status_str += f"winreg SysprepStatus: GeneralizationState=0x{gen:x} ({gen}) CleanupState=0x{clean:x} ({clean})\n"
        except Exception as exc:
            status_str += f"winreg SysprepStatus error: {exc}\n"

        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\Setup\State", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            img_state, _ = winreg.QueryValueEx(key, "ImageState")
            winreg.CloseKey(key)
            status_str += f"winreg ImageState: {img_state}\n"
        except Exception as exc:
            status_str += f"winreg ImageState error: {exc}\n"

    result = subprocess.run(
        [_system_cmd("reg.exe"), "query", r"HKLM\SYSTEM\Setup\Status\SysprepStatus"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    status_str += (result.stdout + result.stderr).strip()
    return status_str.strip()


def _sysprep_completed(status: str) -> bool:
    normalized = status.lower()
    generalization_state = None
    cleanup_state = None

    # Check 1: winreg numeric match
    winreg_match = re.search(r"generalizationstate=0x([0-9a-f]+)\s*\((\d+)\)", normalized)
    if winreg_match:
        try:
            generalization_state = int(winreg_match.group(2))
        except ValueError:
            pass
    cleanup_match = re.search(r"cleanupstate=0x([0-9a-f]+)\s*\((\d+)\)", normalized)
    if cleanup_match:
        try:
            cleanup_state = int(cleanup_match.group(2))
        except ValueError:
            pass

    # Check 2: reg.exe DWORD match
    reg_match = re.search(r"generalizationstate\s+reg_dword\s+(0x[0-9a-f]+|[0-9]+)", normalized)
    if reg_match:
        try:
            generalization_state = int(reg_match.group(1), 0)
        except ValueError:
            pass
    cleanup_reg_match = re.search(r"cleanupstate\s+reg_dword\s+(0x[0-9a-f]+|[0-9]+)", normalized)
    if cleanup_reg_match:
        try:
            cleanup_state = int(cleanup_reg_match.group(1), 0)
        except ValueError:
            pass

    if generalization_state == 7:
        return True

    # Sysprep /generalize /oobe /quit can leave the system resealed for OOBE
    # at state 4 with cleanup complete. Treat that as safe to hand off to WinPE.
    if generalization_state == 4 and cleanup_state == 2:
        return True

    # Check 3: ImageState match
    if "image_state_generalize_reseal_to_oobe" in normalized or "image_state_complete" in normalized:
        return True

    # Check 4: General string containment fallback
    if "generalizationstate" in normalized and ("0x7" in normalized or "0x00000007" in normalized or " 7" in normalized):
        return True

    return False


def _check_sysprep_completion_with_retry(timeout: int = 120, progress_cb: Optional[Callable] = None) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        status = _sysprep_status()
        log.info("Checking Sysprep completion status:\n%s", status)
        if _sysprep_completed(status):
            _safe_progress(progress_cb, f"Sysprep generalization verified in registry:\n{status}")
            return True
        time.sleep(3)
    return False


def _sysprep_failure_hint(logs: str) -> str:
    normalized = logs.lower()
    hints = []
    if "0x80073cf2" in normalized or "0x3cf2" in normalized or "appx" in normalized or "package" in normalized:
        hints.append(
            "Windows reported AppX sysprep failure (0x80073cf2). "
            "This happens when Microsoft Store/AppX packages are installed for a user "
            "but not provisioned for all users, or stuck pending removal."
        )
    if "bitlocker" in normalized or "fve" in normalized:
        hints.append(
            "BitLocker drive encryption is enabled. Run 'manage-bde -off C:' to decrypt C: before running Sysprep."
        )
    if "domain" in normalized or "active directory" in normalized:
        hints.append(
            "Machine is joined to a domain. Unjoin from the domain before running Sysprep /generalize."
        )
    if "reboot" in normalized or "restart" in normalized or "pending" in normalized:
        hints.append(
            "Windows Update has pending operations. Reboot Windows and let updates finish before running Sysprep."
        )
    return "\n\nHint:\n" + "\n".join(hints) if hints else ""


def _extract_sysprep_appx_packages(logs: str) -> list:
    packages = []
    patterns = [
        r"Package\s+([A-Za-z0-9][A-Za-z0-9._-]+_[A-Za-z0-9._-]+__[A-Za-z0-9]+)\s+was installed",
        r"Package\s+([A-Za-z0-9][A-Za-z0-9._-]+_[A-Za-z0-9._-]+__[A-Za-z0-9]+)\s+is installed",
        r"Failed to remove staged package\s+([A-Za-z0-9][A-Za-z0-9._-]+_[A-Za-z0-9._-]+__[A-Za-z0-9]+)",
        r"AppxPackage\s+([A-Za-z0-9][A-Za-z0-9._-]+_[A-Za-z0-9._-]+__[A-Za-z0-9]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, logs, re.IGNORECASE):
            package = match.group(1).strip().rstrip(".;,:()")
            if package not in packages:
                packages.append(package)
    if not packages:
        for match in re.finditer(r"\b([A-Za-z0-9][A-Za-z0-9._-]+_[0-9][A-Za-z0-9._-]*__[A-Za-z0-9]+)\b", logs):
            package = match.group(1).strip().rstrip(".;,:()")
            if package not in packages:
                packages.append(package)
    return packages


def _appx_display_name(package_full_name: str) -> str:
    match = re.match(r"^(.+?)_[0-9]", package_full_name)
    return match.group(1) if match else package_full_name.split("__", 1)[0]


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _remove_sysprep_appx_packages(packages: list, job_id: str, progress_cb: Optional[Callable]):
    if not packages:
        return
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id) or "capture"
    work_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\\ProgramData"), "BretterIMG", "sysprep")
    os.makedirs(work_dir, exist_ok=True)
    script_path = os.path.join(work_dir, f"{safe_job_id}-appx-cleanup.ps1")
    package_entries = "\n".join(
        f"  [pscustomobject]@{{ FullName = {_ps_single_quote(package)}; Name = {_ps_single_quote(_appx_display_name(package))} }}"
        for package in packages
    )
    script = f"""$ErrorActionPreference = 'Continue'
$targets = @(
{package_entries}
)
foreach ($target in $targets) {{
    Write-Output "Bretter-IMG AppX cleanup target: $($target.FullName)"
    $installed = Get-AppxPackage -AllUsers -ErrorAction SilentlyContinue | Where-Object {{
        $_.PackageFullName -eq $target.FullName -or $_.Name -eq $target.Name
    }}
    foreach ($pkg in $installed) {{
        Write-Output "Removing installed AppX package for all users: $($pkg.PackageFullName)"
        try {{
            Remove-AppxPackage -AllUsers -Package $pkg.PackageFullName -ErrorAction Stop
        }} catch {{
            Write-Output "Remove-AppxPackage -AllUsers failed for $($pkg.PackageFullName): $($_.Exception.Message)"
            try {{
                Remove-AppxPackage -Package $pkg.PackageFullName -ErrorAction Stop
            }} catch {{
                Write-Output "Remove-AppxPackage current-user fallback failed for $($pkg.PackageFullName): $($_.Exception.Message)"
            }}
        }}
    }}
    $provisioned = Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Where-Object {{
        $_.PackageName -eq $target.FullName -or $_.DisplayName -eq $target.Name -or $_.PackageName -like "$($target.Name)_*"
    }}
    foreach ($pkg in $provisioned) {{
        Write-Output "Removing provisioned AppX package for all users: $($pkg.PackageName)"
        try {{
            Remove-AppxProvisionedPackage -Online -AllUsers -PackageName $pkg.PackageName -ErrorAction Stop | Out-String | Write-Output
        }} catch {{
            Write-Output "Remove-AppxProvisionedPackage failed for $($pkg.PackageName): $($_.Exception.Message)"
        }}
    }}
}}
"""
    with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(script)
    if progress_cb:
        progress_cb(f"Removing AppX package(s) reported by Sysprep: {', '.join(packages)}")
    output = _run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
        progress_cb=progress_cb,
        timeout=900,
    )
    if progress_cb and output.strip():
        progress_cb("AppX cleanup completed")


def _sysprep_task_info(task_name: str) -> str:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "LIST"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return (result.stdout + result.stderr).strip()


def _sysprep_task_running(task_info: str) -> bool:
    return bool(re.search(r"^\s*Status:\s*Running\s*$", task_info, re.IGNORECASE | re.MULTILINE))


def _wait_for_sysprep_task_stop(
    task_name: str,
    result_path: str,
    progress_cb: Optional[Callable],
    timeout: int = 180,
):
    waited = 0
    while waited < timeout and not os.path.exists(result_path):
        task_info = _sysprep_task_info(task_name)
        if task_info and not _sysprep_task_running(task_info):
            return
        time.sleep(5)
        waited += 5
    if progress_cb and waited >= timeout and not os.path.exists(result_path):
        progress_cb(f"Sysprep task did not report stopped within {timeout} seconds; continuing with separate retry files")


def _stop_sysprep_task(task_name: str, progress_cb: Optional[Callable] = None):
    if progress_cb:
        progress_cb("Stopping failed Sysprep task before AppX cleanup")
    subprocess.run(
        ["schtasks", "/end", "/tn", task_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ["taskkill", "/f", "/im", "Sysprep.exe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    time.sleep(3)


def _run_sysprep_as_system(
    sysprep: str,
    unattended_file_path: str,
    job_id: str,
    progress_cb: Optional[Callable],
    attempt: int = 1,
    timeout_seconds: int = 900,
):
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id) or "capture"
    attempt_suffix = f"a{attempt}-{int(time.time())}"
    task_name = f"BretterIMG-Sysprep-{safe_job_id}-{attempt_suffix}"
    work_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\\ProgramData"), "BretterIMG", "sysprep")
    os.makedirs(work_dir, exist_ok=True)
    result_path = os.path.join(work_dir, f"{safe_job_id}-{attempt_suffix}.result")
    output_path = os.path.join(work_dir, f"{safe_job_id}-{attempt_suffix}.log")
    script_path = os.path.join(work_dir, f"{safe_job_id}-{attempt_suffix}.cmd")
    for path in (result_path, output_path):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    sysprep_dir = os.path.dirname(sysprep) or os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "Sysprep")
    sysprep_args = "/oobe /generalize /quit /quiet"
    if unattended_file_path:
        sysprep_args += f' /unattend:"{unattended_file_path}"'
    with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write(f'cd /d "{sysprep_dir}"\r\n')
        f.write(f'start /wait "" "{sysprep}" {sysprep_args}\r\n')
        f.write(f'echo %errorlevel% > "{result_path}"\r\n')

    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    _run([
        "schtasks", "/create", "/tn", task_name, "/tr", f'cmd.exe /d /c "{script_path}"',
        "/sc", "once", "/st", "00:00", "/ru", "SYSTEM", "/rl", "HIGHEST", "/f",
    ], timeout=30)
    _run(["schtasks", "/run", "/tn", task_name], timeout=30)
    _safe_progress(progress_cb, f"Sysprep SYSTEM scheduled task launched: {task_name}")
    _notify_client_screen(
        "Bretter-IMG: Sysprep generalization is running in the background. "
        "Please do not restart or power off this PC. It will automatically reboot into WinPE when complete."
    )

    waited = 0
    started_at = time.time()
    while not os.path.exists(result_path):
        if waited >= timeout_seconds:
            logs = _sysprep_log_excerpt(started_at)
            _stop_sysprep_task(task_name, progress_cb)
            _run(["schtasks", "/delete", "/tn", task_name, "/f"], timeout=30)
            _notify_client_screen(f"Bretter-IMG: Sysprep did not complete within {timeout_seconds // 60} minutes.")
            raise RuntimeError(
                f"Sysprep did not complete within {timeout_seconds // 60} minutes. Task: {task_name}"
                + (f"\n\n{logs}" if logs else "")
            )
        time.sleep(10)
        waited += 10
        if os.path.exists(result_path):
            break
        if waited % 30 == 0:
            msg = f"Sysprep SYSTEM task is running ({waited // 60}m {waited % 60}s elapsed)..."
            _safe_progress(progress_cb, msg)
            _notify_client_screen(f"Bretter-IMG: Sysprep is generalizing Windows ({waited // 60}m {waited % 60}s elapsed)...")

    with open(result_path, "r", encoding="utf-8", errors="replace") as f:
        result = f.read().strip()
    _run(["schtasks", "/delete", "/tn", task_name, "/f"], timeout=30)

    logs = _sysprep_log_excerpt(started_at)
    sysprep_status = _sysprep_status()

    if result != "0" or not _sysprep_completed(sysprep_status):
        output = ""
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8", errors="replace") as f:
                output = f.read()[-4000:]
        hint = _sysprep_failure_hint(output + logs + sysprep_status)
        raise RuntimeError(
            f"Sysprep failed during generalization (exit code {result}; expected GeneralizationState 7, or 4 with CleanupState 2).\n"
            f"SysprepStatus:\n{sysprep_status or 'unavailable'}\n\n"
            + (f"Panther Error Log:\n{logs}\n\n" if logs else "")
            + (f"Task Output:\n{output}\n\n" if output else "")
            + (f"{hint}" if hint else "")
        )


def _run_sysprep_with_appx_repair(
    sysprep: str,
    unattended_file_path: str,
    job_id: str,
    progress_cb: Optional[Callable],
):
    max_attempts = 3
    repaired_packages = []
    for attempt in range(1, max_attempts + 1):
        _safe_progress(progress_cb, f"Sysprep attempt {attempt} of {max_attempts}")
        try:
            _run_sysprep_as_system(sysprep, unattended_file_path, job_id, progress_cb, attempt=attempt)
            return
        except RuntimeError as exc:
            details = str(exc)
            packages = [
                package for package in _extract_sysprep_appx_packages(details)
                if package not in repaired_packages
            ]
            is_appx_failure = "0x80073cf2" in details.lower() or "0x3cf2" in details.lower()
            if not is_appx_failure or not packages or attempt >= max_attempts:
                raise
            _safe_progress(
                progress_cb,
                "Sysprep reported broken AppX package state; attempting targeted cleanup before retry"
            )
            _remove_sysprep_appx_packages(packages, job_id, progress_cb)
            repaired_packages.extend(packages)
            _safe_progress(progress_cb, "Retrying Sysprep after AppX cleanup")


def _write_vss_helper():
    """Write the PowerShell VSS helper script to the agent directory."""
    script = r"""
param([string]$Volume = "C:\\", [string]$Action = "create")

if ($Action -eq "create") {
    $class = [WMICLASS]"root\cimv2:win32_shadowcopy"
    $result = $class.Create($Volume, "ClientAccessible")
    if ($result.ReturnValue -ne 0) {
        Write-Error "VSS Create failed: $($result.ReturnValue)"
        exit 1
    }
    $shadow = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $result.ShadowID }
    # Output the device path so the caller can mount it
    Write-Output $shadow.DeviceObject
} elseif ($Action -eq "delete") {
    param([string]$ShadowID)
    $shadow = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $ShadowID }
    if ($shadow) { $shadow.Delete() }
}
"""
    with open(VSS_HELPER, "w", encoding="utf-8") as f:
        f.write(script)


def _vss_capture(name: str, source_drive: str, progress_cb: Optional[Callable]) -> str:
    """
    Create a VSS shadow copy of source_drive, mount it, capture with DISM,
    then clean up the shadow copy. Returns path to the WIM file.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    safe_name = name.replace(" ", "_").replace("/", "-")
    wim_path = os.path.join(TEMP_DIR, f"{safe_name}.wim")
    mount_point = os.path.join(TEMP_DIR, "vss_mount")

    # Write helper script
    _write_vss_helper()

    if progress_cb:
        progress_cb("Creating VSS shadow copy (this may take a moment)...")

    # Create the shadow copy
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", VSS_HELPER, "-Volume", source_drive, "-Action", "create"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"VSS creation failed: {result.stdout} {result.stderr}")

    shadow_device = result.stdout.strip()
    if not shadow_device:
        raise RuntimeError("VSS returned empty shadow device path")

    if progress_cb:
        progress_cb(f"VSS shadow created: {shadow_device}")

    # Mount the shadow copy to a directory via symlink
    os.makedirs(mount_point, exist_ok=True)
    # Use mklink to create a directory junction to the shadow volume
    junction_path = mount_point.rstrip("\\") + "\\"
    subprocess.run(["cmd", "/c", f'mklink /d "{mount_point}" "{shadow_device}\\"'],
                   capture_output=True)

    try:
        if progress_cb:
            progress_cb(f"Capturing image from VSS snapshot → {wim_path}")

        _run([
            "dism",
            "/Capture-Image",
            f"/ImageFile:{wim_path}",
            f"/CaptureDir:{mount_point}",
            f"/Name:{name}",
            "/Compress:fast",
            "/CheckIntegrity",
        ], progress_cb=progress_cb, timeout=7200)

    finally:
        # Remove the junction
        subprocess.run(["cmd", "/c", f'rmdir "{mount_point}"'], capture_output=True)

        # Delete the shadow copy
        del_result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command",
             f"Get-WmiObject Win32_ShadowCopy | Where-Object {{ $_.DeviceObject -eq '{shadow_device}' }} | ForEach-Object {{ $_.Delete() }}"],
            capture_output=True, text=True, timeout=60,
        )
        if progress_cb:
            progress_cb("VSS shadow copy cleaned up")

    return wim_path


WINPE_PENDING_FILE = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "BretterIMG", "winpe_pending.json"
)


def _system_drive() -> str:
    drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
    return drive if drive.endswith(":") else "C:"


def _root_stage_dir() -> str:
    return os.environ.get("BRETTER_WINPE_ROOT_STAGE", _system_drive() + r"\BretterIMG\winpe")


def _root_temp_dir() -> str:
    return os.environ.get("BRETTER_ROOT_TEMP", _system_drive() + r"\BretterIMG\temp")


def _stage_dirs() -> list:
    dirs = []
    for path in (WINPE_STAGE_DIR, _root_stage_dir()):
        if path and path not in dirs:
            dirs.append(path)
    return dirs


def _bcd_relative_path(path: str) -> str:
    drive = _system_drive()
    normalized = os.path.normpath(path)
    if normalized.upper().startswith(drive.upper()):
        normalized = normalized[len(drive):]
    return "\\" + normalized.lstrip("\\/")


def _windows_loader_path() -> str:
    try:
        output = _run(["bcdedit", "/enum", "{current}"], timeout=60)
        for entry in _parse_bcd_entries(output):
            path = entry.get("path", "").lower()
            if "winload.exe" in path:
                return r"\Windows\System32\Boot\winload.exe"
    except Exception as e:
        log.info("Could not inspect current Windows loader path: %s", e)
    return r"\Windows\System32\Boot\winload.efi"


def _winpe_boot_descriptions() -> list:
    descriptions = []
    try:
        from transfer import get_winpe_config
        server_description = str(get_winpe_config().get("boot_description", "")).strip()
        if server_description:
            descriptions.append(server_description)
    except Exception as e:
        log.info("Could not read server WinPE boot description: %s", e)

    for description in (WINPE_BOOT_DESCRIPTION, "Bretter-IMG WinPE"):
        description = str(description).strip()
        if description and description not in descriptions:
            descriptions.append(description)
    return descriptions


def _winpe_config() -> dict:
    try:
        from transfer import get_winpe_config
        return get_winpe_config()
    except Exception as e:
        log.info("Could not read server WinPE config: %s", e)
        return {}


def _is_enabled(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _batch_value(value) -> str:
    return str(value or "").replace('"', "'")


def _batch_echo_literal(value: str) -> str:
    text = str(value)
    for old, new in (
        ("^", "^^"),
        ("%", "%%"),
        ("&", "^&"),
        ("|", "^|"),
        ("<", "^<"),
        (">", "^>"),
        ("(", "^("),
        (")", "^)"),
    ):
        text = text.replace(old, new)
    return text


def _winpe_entry_script_content() -> str:
    return """@echo off
setlocal EnableDelayedExpansion
set "WINPE_LOG=X:\\BretterIMG\\winpe-entry.log"
echo Bretter-IMG WinPE launcher started at %DATE% %TIME% > "%WINPE_LOG%"
echo Initializing WinPE... >> "%WINPE_LOG%"
wpeinit >> "%WINPE_LOG%" 2>&1
echo Loading embedded WinPE driver store... >> "%WINPE_LOG%"
if exist "X:\\BretterIMG\\drivers" (
    for /r "X:\\BretterIMG\\drivers" %%I in (*.inf) do drvload "%%I" >> "%WINPE_LOG%" 2>&1
)
echo Rescanning storage after driver load... >> "%WINPE_LOG%"
echo rescan | diskpart >> "%WINPE_LOG%" 2>&1
mountvol /E >nul 2>&1
set DP=X:\\BretterIMG-diskpart.txt
echo automount enable > "%DP%"
echo rescan >> "%DP%"
for %%N in (0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31) do (
  echo select volume %%N >> "%DP%"
  echo assign noerr >> "%DP%"
)
diskpart /s "%DP%" >nul 2>&1
echo Searching for Bretter-IMG operation stage... >> "%WINPE_LOG%"
set STAGE=
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if not defined STAGE if exist "%%D:\\ProgramData\\BretterIMG\\winpe\\winpe-operation.txt" set STAGE=%%D:\\ProgramData\\BretterIMG\\winpe
    if not defined STAGE if exist "%%D:\\BretterIMG\\winpe\\winpe-operation.txt" set STAGE=%%D:\\BretterIMG\\winpe
)
if defined STAGE echo Bretter-IMG stage found: %STAGE%
if defined STAGE echo Bretter-IMG stage found: %STAGE% >> "%WINPE_LOG%"
set OPERATION=
if defined STAGE set /p OPERATION=<"%STAGE%\\winpe-operation.txt"
if defined STAGE if exist "%STAGE%\\drivers" (
    echo Loading external WinPE driver store from %STAGE%\\drivers... >> "%WINPE_LOG%"
    for /r "%STAGE%\\drivers" %%I in (*.inf) do drvload "%%I" >> "%WINPE_LOG%" 2>&1
    echo rescan | diskpart >> "%WINPE_LOG%" 2>&1
)
if /I "%OPERATION%"=="capture" if exist "%STAGE%\\capture.bat" (
  call "%STAGE%\\capture.bat"
  exit /b !errorlevel!
)
if /I "%OPERATION%"=="restore" if exist "%STAGE%\\restore.bat" (
  call "%STAGE%\\restore.bat"
  exit /b !errorlevel!
)
if defined STAGE echo Bretter-IMG WinPE operation "%OPERATION%" is invalid or its script is missing.
set EMBEDDED_OPERATION=
if exist "X:\\BretterIMG\\winpe-operation.txt" set /p EMBEDDED_OPERATION=<"X:\\BretterIMG\\winpe-operation.txt"
if /I "%EMBEDDED_OPERATION%"=="capture" if exist "X:\\BretterIMG\\capture-embedded.bat" (
  echo Bretter-IMG external stage not found; running embedded capture job.
  call "X:\\BretterIMG\\capture-embedded.bat"
  exit /b !errorlevel!
)
if /I "%EMBEDDED_OPERATION%"=="restore" if exist "X:\\BretterIMG\\restore-embedded.bat" (
  echo Bretter-IMG external stage not found; running embedded restore job.
  call "X:\\BretterIMG\\restore-embedded.bat"
  exit /b !errorlevel!
)
if defined EMBEDDED_OPERATION echo Bretter-IMG embedded WinPE operation "%EMBEDDED_OPERATION%" is invalid or its script is missing.
if exist "X:\\BretterIMG\\restore-embedded.bat" (
  echo Bretter-IMG embedded operation marker missing; running legacy embedded restore job.
  call "X:\\BretterIMG\\restore-embedded.bat"
  exit /b !errorlevel!
)
if exist "X:\\BretterIMG\\capture-embedded.bat" (
  echo Bretter-IMG embedded operation marker missing; running legacy embedded capture job.
  call "X:\\BretterIMG\\capture-embedded.bat"
  exit /b !errorlevel!
)
echo Bretter-IMG WinPE stage not found.
echo.
echo Searched C: through Z: for:
echo   \\ProgramData\\BretterIMG\\winpe\\capture.bat
echo   \\ProgramData\\BretterIMG\\winpe\\restore.bat
echo   \\BretterIMG\\winpe\\capture.bat
echo   \\BretterIMG\\winpe\\restore.bat
echo.
echo Volume list:
set LV=X:\\BretterIMG-listvol.txt
echo list volume > "%LV%"
diskpart /s "%LV%"
echo.
echo If the Windows volume is not listed, this WinPE image needs the storage driver for this machine.
cmd
"""


def _write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(content)


def _write_to_stage_dirs(filename: str, content: str) -> list:
    paths = []
    for stage_dir in _stage_dirs():
        path = os.path.join(stage_dir, filename)
        _write_text(path, content)
        paths.append(path)
    return paths


def _capture_log_paths() -> list:
    paths = []
    for stage_dir in _stage_dirs():
        for filename in ("last-capture.log", "dism-capture.log"):
            path = os.path.join(stage_dir, filename)
            if path not in paths:
                paths.append(path)
    return paths


def winpe_capture_read_logs(max_chars: int = 8000) -> str:
    logs = []
    for path in _capture_log_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            logs.append(f"{path}: could not read log: {e}")
            continue
        if content.strip():
            logs.append(f"--- {path} ---\n{content[-max_chars:]}")
    return "\n".join(logs)


def _write_winpe_entry_script():
    _write_to_stage_dirs("winpe-entry.cmd", _winpe_entry_script_content())


def _active_winpe_boot_wim() -> str:
    try:
        if os.path.exists(WINPE_ACTIVE_WIM_FILE):
            with open(WINPE_ACTIVE_WIM_FILE, "r", encoding="utf-8", errors="replace") as f:
                path = f.read().strip()
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return WINPE_BOOT_WIM


def _set_active_winpe_boot_wim(path: str) -> None:
    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)
    with open(WINPE_ACTIVE_WIM_FILE, "w", encoding="utf-8") as f:
        f.write(path)


def _versioned_winpe_boot_wim() -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(WINPE_STAGE_DIR, f"boot-{timestamp}.wim")


def _copy_winpe_assets_from_server(progress_cb: Optional[Callable]) -> str:
    from transfer import download_winpe_asset

    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)
    boot_wim_path = WINPE_BOOT_WIM
    try:
        if progress_cb:
            progress_cb("Copying boot.wim from server to this machine...")
        download_winpe_asset("boot.wim", boot_wim_path, progress_cb=progress_cb)
    except Exception as exc:
        boot_wim_path = _versioned_winpe_boot_wim()
        if progress_cb:
            progress_cb(
                f"Could not replace existing boot.wim ({exc}); staging a fresh boot WIM at {boot_wim_path}"
            )
        download_winpe_asset("boot.wim", boot_wim_path, progress_cb=progress_cb)

    _set_active_winpe_boot_wim(boot_wim_path)
    assets = [("boot.wim", boot_wim_path), ("boot.sdi", WINPE_BOOT_SDI)]
    if progress_cb:
        progress_cb("Copying boot.sdi from server to this machine...")
    download_winpe_asset("boot.sdi", WINPE_BOOT_SDI, progress_cb=progress_cb)

    missing = [path for _, path in assets if not os.path.exists(path) or os.path.getsize(path) == 0]
    if missing:
        raise RuntimeError(
            "WinPE boot files are missing after download: "
            + ", ".join(os.path.basename(path) for path in missing)
        )
    return boot_wim_path


def _copy_winpe_drivers_from_server(progress_cb: Optional[Callable]) -> int:
    from transfer import download_winpe_driver, get_winpe_drivers

    drivers = get_winpe_drivers()
    shutil.rmtree(WINPE_DRIVER_DIR, ignore_errors=True)
    os.makedirs(WINPE_DRIVER_DIR, exist_ok=True)
    for driver in drivers:
        filename = driver.get("filename")
        if not filename:
            continue
        if progress_cb:
            progress_cb(f"Copying WinPE driver {filename} from server...")
        download_winpe_driver(filename, os.path.join(WINPE_DRIVER_DIR, filename), progress_cb=progress_cb)
    _expand_winpe_driver_cabs(progress_cb)
    return len([d for d in drivers if d.get("filename")])


def _expand_winpe_driver_cabs(progress_cb: Optional[Callable]):
    if not os.path.isdir(WINPE_DRIVER_DIR):
        return
    for filename in os.listdir(WINPE_DRIVER_DIR):
        if not filename.lower().endswith(".cab"):
            continue
        cab_path = os.path.join(WINPE_DRIVER_DIR, filename)
        extract_dir = os.path.join(WINPE_DRIVER_DIR, os.path.splitext(filename)[0])
        os.makedirs(extract_dir, exist_ok=True)
        if progress_cb:
            progress_cb(f"Expanding WinPE driver CAB {filename}...")
        _run(["expand", "-F:*", cab_path, extract_dir], progress_cb=progress_cb, timeout=1800)


def _has_winpe_inf_drivers() -> bool:
    if not os.path.isdir(WINPE_DRIVER_DIR):
        return False
    for _, _, files in os.walk(WINPE_DRIVER_DIR):
        if any(filename.lower().endswith(".inf") for filename in files):
            return True
    return False


def _customize_winpe_boot_wim(
    progress_cb: Optional[Callable],
    embedded_files: Optional[dict] = None,
    boot_wim_path: Optional[str] = None,
):
    """
    Inject a tiny Bretter-IMG launcher into boot.wim. WinPE runs from X:, so
    scripts placed next to boot.wim on C: are not enough by themselves.
    """
    mount_dir = os.path.join(WINPE_STAGE_DIR, f"mount-{int(time.time())}-{os.getpid()}")
    boot_wim_path = boot_wim_path or _active_winpe_boot_wim()
    os.makedirs(mount_dir, exist_ok=True)

    if progress_cb:
        progress_cb(f"Mounting {os.path.basename(boot_wim_path)} so Bretter-IMG startup can be injected...")

    mounted = False
    try:
        try:
            _run(["dism", "/Cleanup-Wim"], progress_cb=progress_cb, timeout=300)
        except Exception as cleanup_exc:
            log.info("DISM cleanup before WinPE mount did not complete: %s", cleanup_exc)
            if progress_cb:
                progress_cb(f"Continuing after DISM cleanup warning: {cleanup_exc}")
        _run([
            "dism",
            "/Mount-Image",
            f"/ImageFile:{boot_wim_path}",
            "/Index:1",
            f"/MountDir:{mount_dir}",
        ], progress_cb=progress_cb, timeout=1800)
        mounted = True

        entry_dir = os.path.join(mount_dir, "BretterIMG")
        system32_dir = os.path.join(mount_dir, "Windows", "System32")
        os.makedirs(entry_dir, exist_ok=True)
        os.makedirs(system32_dir, exist_ok=True)

        embedded_drivers = os.path.join(entry_dir, "drivers")
        shutil.rmtree(embedded_drivers, ignore_errors=True)
        if os.path.isdir(WINPE_DRIVER_DIR):
            shutil.copytree(WINPE_DRIVER_DIR, embedded_drivers)

        _write_text(os.path.join(entry_dir, "winpe-entry.cmd"), _winpe_entry_script_content())
        for stale_filename in ("capture-embedded.bat", "restore-embedded.bat", "winpe-operation.txt"):
            stale_path = os.path.join(entry_dir, stale_filename)
            if os.path.exists(stale_path):
                os.remove(stale_path)
        for filename, content in (embedded_files or {}).items():
            _write_text(os.path.join(entry_dir, filename), content)

        with open(os.path.join(system32_dir, "winpeshl.ini"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write("[LaunchApps]\n")
            f.write("%SYSTEMROOT%\\System32\\cmd.exe, /c X:\\BretterIMG\\winpe-entry.cmd\n")

        if _has_winpe_inf_drivers():
            if progress_cb:
                progress_cb("Injecting WinPE storage drivers into boot.wim...")
            _run([
                "dism",
                f"/Image:{mount_dir}",
                "/Add-Driver",
                f"/Driver:{WINPE_DRIVER_DIR}",
                "/Recurse",
            ], progress_cb=progress_cb, timeout=1800)

        if progress_cb:
            progress_cb("Committing Bretter-IMG startup into boot.wim...")
        _run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Commit"], progress_cb=progress_cb, timeout=1800)
        mounted = False
    except Exception:
        if mounted:
            try:
                _run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"], timeout=1800)
            except Exception as cleanup_error:
                log.warning("Failed to discard mounted WinPE image: %s", cleanup_error)
        raise
    finally:
        shutil.rmtree(mount_dir, ignore_errors=True)


def _create_or_update_winpe_bcd_entry(
    progress_cb: Optional[Callable],
    boot_wim_path: Optional[str] = None,
) -> str:
    drive = _system_drive()
    boot_description = _winpe_boot_descriptions()[0]
    boot_wim_path = boot_wim_path or _active_winpe_boot_wim()
    boot_wim_bcd = f"ramdisk=[{drive}]{_bcd_relative_path(boot_wim_path)},{{ramdiskoptions}}"
    boot_sdi_bcd = _bcd_relative_path(WINPE_BOOT_SDI)

    try:
        _run(["bcdedit", "/set", "{ramdiskoptions}", "ramdisksdidevice", f"partition={drive}"], timeout=60)
    except Exception:
        _run(["bcdedit", "/create", "{ramdiskoptions}", "/d", "Bretter-IMG Ramdisk Options"], timeout=60)
        _run(["bcdedit", "/set", "{ramdiskoptions}", "ramdisksdidevice", f"partition={drive}"], timeout=60)
    _run(["bcdedit", "/set", "{ramdiskoptions}", "ramdisksdipath", boot_sdi_bcd], timeout=60)

    boot_id = _find_winpe_boot_id_optional(progress_cb)
    if not boot_id:
        output = _run([
            "bcdedit",
            "/create",
            "/d",
            boot_description,
            "/application",
            "osloader",
        ], timeout=60)
        match = re.search(r"\{[0-9a-fA-F-]{36}\}", output)
        if not match:
            raise RuntimeError(f"Could not parse new WinPE BCD identifier from: {output}")
        boot_id = match.group(0)
        if progress_cb:
            progress_cb(f"Created WinPE BCD entry {boot_id}")

    bcd_settings = [
        ("device", boot_wim_bcd),
        ("osdevice", boot_wim_bcd),
        ("path", _windows_loader_path()),
        ("systemroot", r"\Windows"),
        ("winpe", "yes"),
        ("detecthal", "yes"),
    ]
    for key, value in bcd_settings:
        _run(["bcdedit", "/set", boot_id, key, value], timeout=60)

    try:
        _run(["bcdedit", "/displayorder", boot_id, "/addlast"], timeout=60)
    except Exception as e:
        log.info("Could not add WinPE entry to display order: %s", e)

    if progress_cb:
        progress_cb(f"WinPE BCD entry ready: {boot_id}")
    return boot_id


def _winpe_capture_phase1(
    name: str,
    job_id: str,
    source_drive: str,
    wim_path: str,
    progress_cb: Optional[Callable],
    direct_capture_credentials: Optional[dict] = None,
    schedule_reboot: bool = True,
    embed_script: bool = True,
    require_sysprep_complete: bool = False,
):
    """
    Phase 1: Stage WinPE capture scripts and schedule a reboot.
    Writes winpe_pending.json so the agent picks up the upload after reboot.
    Raises WinPERebootPending — caller must NOT attempt to upload yet.
    """
    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    safe_batch_name = name.replace('"', "'")
    safe_wim_filename = os.path.basename(wim_path).replace('"', "'")
    config = _winpe_config()
    direct_enabled = _is_enabled(config.get("direct_capture_enabled")) and bool(config.get("direct_capture_share"))
    direct_filename = _safe_capture_filename(name)
    direct_drive = _batch_value(config.get("direct_capture_drive") or "Z:")
    if len(direct_drive) == 1:
        direct_drive = direct_drive + ":"
    direct_share = _batch_value(config.get("direct_capture_share", ""))
    direct_capture_credentials = direct_capture_credentials or {}
    direct_username = _batch_value(
        direct_capture_credentials.get("username") or config.get("direct_capture_username", "")
    )
    direct_password = _batch_value(
        direct_capture_credentials.get("password") or config.get("direct_capture_password", "")
    )
    if direct_enabled:
        direct_target = f"{direct_drive}\\{direct_filename}"
        direct_base, direct_ext = os.path.splitext(direct_filename)
        direct_work_filename = f"{direct_base}.{job_id}.partial{direct_ext or '.wim'}"
        if direct_username:
            direct_map = f"""
call :DIRECT_MAP_WITH_RETRY
"""
        else:
            direct_map = f"""
reg add HKLM\\SYSTEM\\CurrentControlSet\\Services\\LanmanWorkstation\\Parameters /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f >> "%CAPTURE_LOG%" 2>&1
set "DIRECT_USER=Guest"
set "DIRECT_PASS="
call :DIRECT_MAP_WITH_RETRY
"""
        direct_capture_block = f"""
set DIRECT_CAPTURE=1
set "DIRECT_SHARE={direct_share}"
set "DIRECT_USER={direct_username}"
set "DIRECT_PASS={direct_password}"
set "DIRECT_HOST=%DIRECT_SHARE%"
set "DIRECT_HOST=%DIRECT_HOST:~2%"
for /f "tokens=1 delims=\\" %%S in ("%DIRECT_HOST%") do set "DIRECT_HOST=%%S"
set "DIRECT_SHARE_LEAF=%DIRECT_SHARE%"
set "DIRECT_SHARE_LEAF=%DIRECT_SHARE_LEAF:~2%"
for /f "tokens=2 delims=\\" %%S in ("%DIRECT_SHARE_LEAF%") do set "DIRECT_SHARE_LEAF=%%S"
set DIRECT_NETBIOS=
set DIRECT_ALT_SHARE=
net use {direct_drive} /delete /y >nul 2>&1
echo Loading staged WinPE network/storage drivers, if available...
echo Loading staged WinPE network/storage drivers, if available... >> "%CAPTURE_LOG%"
if exist "%STAGE_DIR%drivers" (
  for /r "%STAGE_DIR%drivers" %%I in (*.inf) do drvload "%%I" >> "%CAPTURE_LOG%" 2>&1
)
echo Initializing WinPE networking...
echo Initializing WinPE networking... >> "%CAPTURE_LOG%"
wpeutil InitializeNetwork >> "%CAPTURE_LOG%" 2>&1
netcfg -winpe >> "%CAPTURE_LOG%" 2>&1
net start dhcp >> "%CAPTURE_LOG%" 2>&1
net start lmhosts >> "%CAPTURE_LOG%" 2>&1
net start lanmanworkstation >> "%CAPTURE_LOG%" 2>&1
ipconfig /renew >> "%CAPTURE_LOG%" 2>&1
echo Preparing WinPE network for SMB host %DIRECT_HOST%...
echo Preparing WinPE network for SMB host %DIRECT_HOST%... >> "%CAPTURE_LOG%"
ipconfig
ipconfig /all >> "%CAPTURE_LOG%" 2>&1
for /L %%R in (1,1,12) do (
  ping -n 1 -w 3000 "%DIRECT_HOST%" >nul 2>&1 && goto DIRECT_HOST_READY
  echo Waiting for SMB host %DIRECT_HOST% attempt %%R of 12...
  echo Waiting for SMB host %DIRECT_HOST% attempt %%R of 12... >> "%CAPTURE_LOG%"
  ping -n 6 127.0.0.1 >nul
)
:DIRECT_HOST_READY
ping -n 1 -w 3000 "%DIRECT_HOST%" >nul 2>&1
if errorlevel 1 (
  echo WARNING: SMB host %DIRECT_HOST% did not answer ping; trying SMB mapping anyway.
  echo WARNING: SMB host %DIRECT_HOST% did not answer ping; trying SMB mapping anyway. >> "%CAPTURE_LOG%"
  ipconfig /all
)
{direct_map}
:DIRECT_MAP_OK
if not "%MAP_RC%"=="0" (
  echo ERROR: Could not map server image share %DIRECT_SHARE%. NET USE error %MAP_RC%.
  echo ERROR: Could not map server image share %DIRECT_SHARE%. NET USE error %MAP_RC%. >> "%CAPTURE_LOG%"
  echo User attempted: %DIRECT_USER%
  net use
  net use >> "%CAPTURE_LOG%" 2>&1
  echo.
  echo SMB direct capture failed; local fallback is disabled for direct SMB jobs.
  echo SMB direct capture failed; local fallback is disabled for direct SMB jobs. >> "%CAPTURE_LOG%"
  goto DIRECT_MAP_FAILED
)
echo SMB share mapped to {direct_drive}. >> "%CAPTURE_LOG%"
set WIM_FINAL={direct_drive}\\{direct_filename}
set WIM_REMOTE={direct_drive}\\{direct_work_filename}
set WIM=%WIM_REMOTE%
set WIM_FINAL_PREEXIST=0
if exist "%WIM_FINAL%" (
  set WIM_FINAL_PREEXIST=1
  echo Existing final image will be preserved unless this capture completes: %WIM_FINAL%
  echo Existing final image will be preserved unless this capture completes: %WIM_FINAL% >> "%CAPTURE_LOG%"
)
:DIRECT_CAPTURE_READY
"""
    else:
        direct_target = wim_path
        direct_capture_block = f"""
set DIRECT_CAPTURE=0
set OUT_DRIVE=%RUN_DRIVE%
if /I "%OUT_DRIVE%"=="X:" set OUT_DRIVE=%SRC%
set WIM=%OUT_DRIVE%\\ProgramData\\BretterIMG\\temp\\{safe_wim_filename}
if not exist "%OUT_DRIVE%\\ProgramData\\BretterIMG\\temp" mkdir "%OUT_DRIVE%\\ProgramData\\BretterIMG\\temp"
if not exist "%OUT_DRIVE%\\ProgramData\\BretterIMG\\temp" (
  if not exist "%OUT_DRIVE%\\BretterIMG\\temp" mkdir "%OUT_DRIVE%\\BretterIMG\\temp"
  set WIM=%OUT_DRIVE%\\BretterIMG\\temp\\{safe_wim_filename}
)
"""
    require_sysprep_value = "1" if require_sysprep_complete else "0"
    capture_content = f"""@echo off
setlocal DisableDelayedExpansion
echo Bretter-IMG WinPE: Capturing {safe_batch_name} ...
set RUN_DRIVE=%~d0
set "STAGE_DIR=%~dp0"
set REQUIRE_SYSPREP_COMPLETE={require_sysprep_value}
set SRC=
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do if not defined SRC if exist "%%D:\\Windows\\System32\\config\\SYSTEM" if exist "%%D:\\Users" set SRC=%%D:
if not defined SRC (
  echo ERROR: Windows source volume not found.
  echo This WinPE image may need the storage driver for this machine.
  pause
  exit /b 1
)
if not exist "%SRC%\\ProgramData\\BretterIMG\\winpe" mkdir "%SRC%\\ProgramData\\BretterIMG\\winpe"
set "CAPTURE_LOG=%SRC%\\ProgramData\\BretterIMG\\winpe\\last-capture.log"
set "DISM_LOG=%SRC%\\ProgramData\\BretterIMG\\winpe\\dism-capture.log"
set "WIM_SCRIPT=%SRC%\\ProgramData\\BretterIMG\\winpe\\wimscript.ini"
echo Bretter-IMG WinPE capture started at %DATE% %TIME% > "%CAPTURE_LOG%"
echo RUN_DRIVE=%RUN_DRIVE% SRC=%SRC% >> "%CAPTURE_LOG%"
if "%REQUIRE_SYSPREP_COMPLETE%"=="1" (
  call :VERIFY_SYSPREP_COMPLETE
  if errorlevel 1 goto SYSPREP_NOT_COMPLETE
)
{direct_capture_block}
if not defined WIM_FINAL set WIM_FINAL=%WIM%
echo Capturing %SRC%\\ to %WIM%
echo Capturing %SRC%\\ to %WIM% >> "%CAPTURE_LOG%"
if "%DIRECT_CAPTURE%"=="1" (
  echo Direct SMB capture will write directly to %WIM%, then finalize as %WIM_FINAL%.
  echo Direct SMB capture will write directly to %WIM%, then finalize as %WIM_FINAL%. >> "%CAPTURE_LOG%"
  call :LOG_TARGET_FREE_SPACE
)
if exist "%WIM%" (
  echo Removing stale capture working file: %WIM%
  echo Removing stale capture working file: %WIM% >> "%CAPTURE_LOG%"
  del /f /q "%WIM%" >> "%CAPTURE_LOG%" 2>&1
)
call :WRITE_WIM_SCRIPT
dism /Capture-Image /ImageFile:"%WIM%" /CaptureDir:%SRC%\\ /Name:"{safe_batch_name}" /Compress:fast /CheckIntegrity /ConfigFile:"%WIM_SCRIPT%" /LogPath:"%DISM_LOG%" /LogLevel:4
set DISM_RC=%errorlevel%
echo DISM exited with code %DISM_RC%. Detailed DISM log: %DISM_LOG%
echo DISM exited with code %DISM_RC%. Detailed DISM log: %DISM_LOG% >> "%CAPTURE_LOG%"
if not "%DISM_RC%"=="0" goto CAPTURE_FAILED
if not exist "%WIM%" goto CAPTURE_MISSING
set WIM_SIZE=0
for %%A in ("%WIM%") do set WIM_SIZE=%%~zA
if %WIM_SIZE% LSS 1048576 goto CAPTURE_TOO_SMALL
if "%DIRECT_CAPTURE%"=="1" (
  call :DIRECT_FINALIZE_SMB
  if errorlevel 1 goto DIRECT_FINALIZE_FAILED
)
if "%DIRECT_CAPTURE%"=="1" net use {direct_drive} /delete /y >nul 2>&1
echo Capture complete at %DATE% %TIME%. >> "%CAPTURE_LOG%"
echo Capture complete. Rebooting back to Windows...
wpeutil reboot
exit /b 0

:WRITE_WIM_SCRIPT
echo Writing DISM capture exclusion file: %WIM_SCRIPT%
echo Writing DISM capture exclusion file: %WIM_SCRIPT% >> "%CAPTURE_LOG%"
> "%WIM_SCRIPT%" echo [ExclusionList]
>> "%WIM_SCRIPT%" echo \\hiberfil.sys
>> "%WIM_SCRIPT%" echo \\pagefile.sys
>> "%WIM_SCRIPT%" echo \\swapfile.sys
>> "%WIM_SCRIPT%" echo \\System Volume Information
>> "%WIM_SCRIPT%" echo \\$Recycle.Bin
>> "%WIM_SCRIPT%" echo \\Windows\\CSC
>> "%WIM_SCRIPT%" echo \\ProgramData\\BretterIMG\\temp
>> "%WIM_SCRIPT%" echo \\BretterIMG\\temp
>> "%WIM_SCRIPT%" echo \\ProgramData\\anaconda3
>> "%WIM_SCRIPT%" echo.
>> "%WIM_SCRIPT%" echo [CompressionExclusionList]
>> "%WIM_SCRIPT%" echo *.zip
>> "%WIM_SCRIPT%" echo *.cab
>> "%WIM_SCRIPT%" echo *.wim
>> "%WIM_SCRIPT%" echo *.esd
type "%WIM_SCRIPT%" >> "%CAPTURE_LOG%" 2>&1
exit /b 0

:VERIFY_SYSPREP_COMPLETE
set GEN_STATE=
set CLEAN_STATE=
set OFFLINE_SYSTEM=HKLM\\BretterIMG_Offline_SYSTEM_%RANDOM%%RANDOM%
echo Verifying offline SysprepStatus before capture... >> "%CAPTURE_LOG%"
reg load "%OFFLINE_SYSTEM%" "%SRC%\\Windows\\System32\\config\\SYSTEM" >> "%CAPTURE_LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Could not load offline SYSTEM registry hive from %SRC%. >> "%CAPTURE_LOG%"
  exit /b 1
)
for /f "tokens=3" %%A in ('reg query "%OFFLINE_SYSTEM%\\Setup\\Status\\SysprepStatus" /v GeneralizationState 2^>nul ^| find /I "GeneralizationState"') do set GEN_STATE=%%A
for /f "tokens=3" %%A in ('reg query "%OFFLINE_SYSTEM%\\Setup\\Status\\SysprepStatus" /v CleanupState 2^>nul ^| find /I "CleanupState"') do set CLEAN_STATE=%%A
reg unload "%OFFLINE_SYSTEM%" >> "%CAPTURE_LOG%" 2>&1
echo Offline SysprepStatus: GeneralizationState=%GEN_STATE% CleanupState=%CLEAN_STATE%
echo Offline SysprepStatus: GeneralizationState=%GEN_STATE% CleanupState=%CLEAN_STATE% >> "%CAPTURE_LOG%"
if /I "%GEN_STATE%"=="0x7" exit /b 0
if /I "%GEN_STATE%"=="0x00000007" exit /b 0
if /I "%GEN_STATE%"=="0x4" if /I "%CLEAN_STATE%"=="0x2" exit /b 0
if /I "%GEN_STATE%"=="0x00000004" if /I "%CLEAN_STATE%"=="0x00000002" exit /b 0
set GEN_NUM=0
set CLEAN_NUM=0
set /a GEN_NUM=%GEN_STATE% 2>nul
set /a CLEAN_NUM=%CLEAN_STATE% 2>nul
if "%GEN_NUM%"=="7" exit /b 0
if "%GEN_NUM%"=="4" if "%CLEAN_NUM%"=="2" exit /b 0
echo ERROR: Sysprep was requested, but offline Windows does not report completed generalization.
echo ERROR: Sysprep was requested, but offline Windows does not report completed generalization. >> "%CAPTURE_LOG%"
exit /b 1

:LOG_TARGET_FREE_SPACE
set TARGET_DRIVE=
for %%T in ("%WIM%") do set "TARGET_DRIVE=%%~dT"
if not defined TARGET_DRIVE exit /b 0
echo Target free space for %TARGET_DRIVE%\\ before capture: >> "%CAPTURE_LOG%"
fsutil volume diskfree %TARGET_DRIVE%\\ >> "%CAPTURE_LOG%" 2>&1
exit /b 0

:DELETE_DIRECT_CAPTURE_FILES
if not "%DIRECT_CAPTURE%"=="1" exit /b 0
echo Cleaning up only this failed capture's SMB image files. >> "%CAPTURE_LOG%"
if defined WIM_REMOTE if exist "%WIM_REMOTE%" (
  echo Deleting failed capture working image: %WIM_REMOTE%
  echo Deleting failed capture working image: %WIM_REMOTE% >> "%CAPTURE_LOG%"
  del /f /q "%WIM_REMOTE%" >> "%CAPTURE_LOG%" 2>&1
)
if not "%WIM_FINAL_PREEXIST%"=="1" if defined WIM_FINAL if exist "%WIM_FINAL%" (
  echo Deleting failed capture final image: %WIM_FINAL%
  echo Deleting failed capture final image: %WIM_FINAL% >> "%CAPTURE_LOG%"
  del /f /q "%WIM_FINAL%" >> "%CAPTURE_LOG%" 2>&1
)
exit /b 0

:DIRECT_MAP_WITH_RETRY
set MAP_RC=1
set DIRECT_MAP_ATTEMPT=0
:DIRECT_MAP_RETRY
set /a DIRECT_MAP_ATTEMPT+=1
echo SMB map attempt %DIRECT_MAP_ATTEMPT% of 18 for %DIRECT_SHARE% as %DIRECT_USER%...
echo SMB map attempt %DIRECT_MAP_ATTEMPT% of 18 for %DIRECT_SHARE% as %DIRECT_USER%... >> "%CAPTURE_LOG%"
call :DIRECT_TRY_MAP "%DIRECT_SHARE%" "%DIRECT_USER%"
if not errorlevel 1 exit /b 0
echo SMB map failed with error %MAP_RC%; trying server-qualified username.
echo SMB map failed with error %MAP_RC%; trying server-qualified username. >> "%CAPTURE_LOG%"
call :DIRECT_TRY_MAP "%DIRECT_SHARE%" "%DIRECT_HOST%\\%DIRECT_USER%"
if not errorlevel 1 exit /b 0
if "%DIRECT_MAP_ATTEMPT%"=="1" call :DIRECT_SMB_DIAGNOSTICS
if defined DIRECT_ALT_SHARE (
  echo SMB map failed; trying NetBIOS path %DIRECT_ALT_SHARE%.
  echo SMB map failed; trying NetBIOS path %DIRECT_ALT_SHARE%. >> "%CAPTURE_LOG%"
  call :DIRECT_TRY_MAP "%DIRECT_ALT_SHARE%" "%DIRECT_USER%"
  if not errorlevel 1 exit /b 0
  call :DIRECT_TRY_MAP "%DIRECT_ALT_SHARE%" "%DIRECT_NETBIOS%\\%DIRECT_USER%"
  if not errorlevel 1 exit /b 0
)
if %DIRECT_MAP_ATTEMPT% LSS 18 (
  ping -n 11 127.0.0.1 >nul
  goto DIRECT_MAP_RETRY
)
exit /b %MAP_RC%

:DIRECT_TRY_MAP
net use {direct_drive} /delete /y >nul 2>&1
echo net use {direct_drive} "%~1" /user:"%~2" >> "%CAPTURE_LOG%"
net use {direct_drive} "%~1" "%DIRECT_PASS%" /user:"%~2" /persistent:no >> "%CAPTURE_LOG%" 2>&1
set MAP_RC=%errorlevel%
exit /b %MAP_RC%

:DIRECT_SMB_DIAGNOSTICS
echo SMB diagnostics for %DIRECT_HOST% at %DATE% %TIME% >> "%CAPTURE_LOG%"
route print >> "%CAPTURE_LOG%" 2>&1
arp -a >> "%CAPTURE_LOG%" 2>&1
nbtstat -A "%DIRECT_HOST%" >> "%CAPTURE_LOG%" 2>&1
for /f "tokens=1" %%N in ('nbtstat -A "%DIRECT_HOST%" ^| find "<20>"') do if not defined DIRECT_NETBIOS set "DIRECT_NETBIOS=%%N"
if defined DIRECT_NETBIOS set "DIRECT_ALT_SHARE=\\\\%DIRECT_NETBIOS%\\%DIRECT_SHARE_LEAF%"
if defined DIRECT_ALT_SHARE echo NetBIOS SMB fallback path: %DIRECT_ALT_SHARE% >> "%CAPTURE_LOG%"
net view "\\\\%DIRECT_HOST%" >> "%CAPTURE_LOG%" 2>&1
if defined DIRECT_NETBIOS net view "\\\\%DIRECT_NETBIOS%" >> "%CAPTURE_LOG%" 2>&1
if exist "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; $r=$c.BeginConnect('%DIRECT_HOST%',445,$null,$null); if($r.AsyncWaitHandle.WaitOne(5000)){{$c.EndConnect($r); 'TCP 445 reachable'}}else{{'TCP 445 timed out'; exit 1}}; $c.Close()" >> "%CAPTURE_LOG%" 2>&1
) else (
  echo PowerShell unavailable; skipped TCP 445 probe. >> "%CAPTURE_LOG%"
)
exit /b 0

:DIRECT_FINALIZE_SMB
if not exist "%WIM_REMOTE%" (
  echo ERROR: Direct SMB capture working file does not exist: %WIM_REMOTE%.
  echo ERROR: Direct SMB capture working file does not exist: %WIM_REMOTE%. >> "%CAPTURE_LOG%"
  exit /b 1
)
echo Finalizing SMB image %WIM_FINAL%
echo Finalizing SMB image %WIM_FINAL% >> "%CAPTURE_LOG%"
move /y "%WIM_REMOTE%" "%WIM_FINAL%" >> "%CAPTURE_LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: SMB finalize failed.
  echo ERROR: SMB finalize failed. >> "%CAPTURE_LOG%"
  exit /b 1
)
if not exist "%WIM_FINAL%" (
  echo ERROR: SMB finalize reported success but %WIM_FINAL% does not exist.
  echo ERROR: SMB finalize reported success but %WIM_FINAL% does not exist. >> "%CAPTURE_LOG%"
  exit /b 1
)
exit /b 0

:DIRECT_MAP_FAILED
echo ERROR: Direct SMB capture cannot continue because the share is unavailable.
echo ERROR: Direct SMB capture cannot continue because the share is unavailable. >> "%CAPTURE_LOG%"
wpeutil reboot
exit /b %MAP_RC%

:SYSPREP_NOT_COMPLETE
echo ERROR: Capture aborted because Sysprep did not complete successfully.
echo ERROR: Capture aborted because Sysprep did not complete successfully. >> "%CAPTURE_LOG%"
call :DELETE_DIRECT_CAPTURE_FILES
wpeutil reboot
exit /b 1

:CAPTURE_FAILED
echo ERROR: DISM failed code %DISM_RC%.
echo ERROR: DISM failed code %DISM_RC%. >> "%CAPTURE_LOG%"
if "%DIRECT_CAPTURE%"=="1" echo Direct SMB working path was %WIM%
if "%DIRECT_CAPTURE%"=="1" echo Direct SMB working path was %WIM% >> "%CAPTURE_LOG%"
call :DELETE_DIRECT_CAPTURE_FILES
wpeutil reboot
exit /b %DISM_RC%

:CAPTURE_MISSING
echo ERROR: DISM reported success but no image file exists at %WIM%.
echo ERROR: DISM reported success but no image file exists at %WIM%. >> "%CAPTURE_LOG%"
call :DELETE_DIRECT_CAPTURE_FILES
wpeutil reboot
exit /b 1

:CAPTURE_TOO_SMALL
echo ERROR: Captured image is too small to be valid: %WIM% (%WIM_SIZE% bytes).
echo ERROR: Captured image is too small to be valid: %WIM% (%WIM_SIZE% bytes). >> "%CAPTURE_LOG%"
call :DELETE_DIRECT_CAPTURE_FILES
wpeutil reboot
exit /b 1

:DIRECT_FINALIZE_FAILED
echo ERROR: DISM completed, but the direct SMB finalize failed.
echo ERROR: DISM completed, but the direct SMB finalize failed. >> "%CAPTURE_LOG%"
call :DELETE_DIRECT_CAPTURE_FILES
wpeutil reboot
exit /b 1
"""
    capture_paths = _write_to_stage_dirs("capture.bat", capture_content)
    _write_to_stage_dirs("winpe-operation.txt", "capture\n")

    _safe_progress(progress_cb, f"WinPE capture script staged: {', '.join(capture_paths)}")
    if embed_script:
        _safe_progress(progress_cb, "Embedding capture script into boot.wim as a fallback...")
    else:
        _safe_progress(progress_cb, "Using the WinPE launcher already staged by Push WinPE")
    if embed_script:
        _customize_winpe_boot_wim(progress_cb, {
            "winpe-operation.txt": "capture\n",
            "capture-embedded.bat": capture_content,
        })

    _safe_progress(progress_cb, f"WIM will be saved to: {direct_target}")
    if schedule_reboot:
        _safe_progress(progress_cb, "Preparing one-time WinPE boot for capture...")
    elif require_sysprep_complete:
        _safe_progress(progress_cb, "Preparing guarded WinPE capture handoff; WinPE will verify Sysprep before capture")
    else:
        _safe_progress(progress_cb, "Preparing WinPE capture handoff files")

    if schedule_reboot:
        _schedule_winpe_reboot(
            "Bretter-IMG: Rebooting into WinPE to capture image",
            15,
            progress_cb,
            screen_message="Bretter-IMG: Rebooting into WinPE to capture image...",
        )


class WinPERebootPending(Exception):
    """Raised after a job has been handed off to WinPE and must stay running."""
    def __init__(self, message: str, wim_path: Optional[str] = None):
        self.wim_path = wim_path
        super().__init__(message)


def winpe_capture_start(
    name: str,
    job_id: str,
    machine_id: str,
    source_drive: str = "C:\\",
    progress_cb: Optional[Callable] = None,
    direct_capture_credentials: Optional[dict] = None,
    sysprep_before_capture: bool = False,
    unattended_file_path: str = "",
):
    """
    Start a WinPE capture (Phase 1). Saves pending state and reboots.
    Raises WinPERebootPending — agent must save job as 'running' and exit.
    On next boot the agent calls winpe_capture_resume() to finish.
    """
    safe_name = name.replace(" ", "_").replace("/", "-")
    wim_path = os.path.join(TEMP_DIR, f"{safe_name}.wim")

    # Save state so we can resume after reboot. If boot preparation fails, remove
    # the state file so the next agent startup can return to normal polling.
    import json
    state = {
        "job_id": job_id,
        "machine_id": machine_id,
        "name": name,
        "wim_path": wim_path,
        "alternate_wim_paths": [os.path.join(_root_temp_dir(), os.path.basename(wim_path))],
        "winpe_log_paths": _capture_log_paths(),
    }
    config = _winpe_config()
    if _is_enabled(config.get("direct_capture_enabled")) and config.get("direct_capture_share"):
        state["direct_capture"] = {
            "enabled": True,
            "filename": _safe_capture_filename(name),
        }
    os.makedirs(os.path.dirname(WINPE_PENDING_FILE), exist_ok=True)
    with open(WINPE_PENDING_FILE, "w") as f:
        json.dump(state, f)

    try:
        if sysprep_before_capture:
            _winpe_capture_phase1(
                name, job_id, source_drive, wim_path, progress_cb, direct_capture_credentials,
                schedule_reboot=False, embed_script=False, require_sysprep_complete=True,
            )
            if unattended_file_path and not os.path.isfile(unattended_file_path):
                raise RuntimeError(f"Unattended file was not found: {unattended_file_path}")
            if unattended_file_path:
                try:
                    ET.parse(unattended_file_path)
                except (OSError, ET.ParseError) as exc:
                    raise RuntimeError(f"Unattended file is not valid XML: {exc}") from exc
            boot_id = _find_winpe_boot_id(progress_cb)
            _safe_progress(progress_cb, "Staging WinPE boot entry before Sysprep...")
            _prepare_winpe_boot(progress_cb)
            sysprep = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "Sysprep", "Sysprep.exe")
            _safe_progress(progress_cb, "Starting Sysprep as a SYSTEM scheduled task and waiting for generalization")
            _run_sysprep_with_appx_repair(sysprep, unattended_file_path, job_id, progress_cb)
            if not _check_sysprep_completion_with_retry(timeout=60, progress_cb=progress_cb):
                sysprep_status = _sysprep_status()
                raise RuntimeError(
                    "Sysprep exited but Windows did not report a completed generalization state. "
                    f"SysprepStatus:\n{sysprep_status or 'unavailable'}"
                )
            _safe_progress(progress_cb, "Sysprep completed and Windows is generalized; refreshing WinPE BCD entry...")
            boot_id = _create_or_update_winpe_bcd_entry(progress_cb)
            _safe_progress(progress_cb, f"Scheduling one-time boot to WinPE entry {boot_id} and rebooting...")
            _schedule_winpe_reboot(
                "Bretter-IMG: Sysprep complete, rebooting into WinPE to capture image",
                5,
                progress_cb,
                boot_id=boot_id,
                screen_message="Bretter-IMG: Sysprep completed successfully! Rebooting into WinPE to capture image...",
            )
        else:
            _winpe_capture_phase1(name, job_id, source_drive, wim_path, progress_cb, direct_capture_credentials)
    except Exception as exc:
        _notify_client_screen(f"Bretter-IMG: Sysprep capture failed: {exc}")
        reset_next_boot_to_windows(progress_cb)
        winpe_capture_clear()
        raise
    raise WinPERebootPending(f"Rebooting into WinPE — WIM will be at {wim_path}", wim_path)


def winpe_capture_resume():
    """
    Phase 2 (called on agent startup after reboot).
    Returns (job_id, machine_id, name, wim_path) if a pending capture exists, else None.
    """
    import json
    if not os.path.exists(WINPE_PENDING_FILE):
        return None
    try:
        with open(WINPE_PENDING_FILE) as f:
            state = json.load(f)
        return state
    except Exception as e:
        log.warning(f"Could not read winpe_pending.json: {e}")
        return None


def winpe_capture_clear():
    """Remove the pending capture state file after successful upload."""
    if os.path.exists(WINPE_PENDING_FILE):
        os.remove(WINPE_PENDING_FILE)


def _windows_boot_epoch() -> Optional[float]:
    if os.name != "nt":
        return None
    try:
        uptime_seconds = ctypes.windll.kernel32.GetTickCount64() / 1000
        return time.time() - uptime_seconds
    except Exception:
        return None


def _winpe_deploy_state(job_id: str, machine_id: str, image_filename: str, post_deploy: Optional[dict] = None) -> dict:
    return {
        "operation": "deploy",
        "job_id": job_id,
        "machine_id": machine_id,
        "image_filename": image_filename,
        "handoff_created_at": time.time(),
        "handoff_boot_epoch": _windows_boot_epoch(),
        "result_paths": [os.path.join(path, "deploy-result.txt") for path in _stage_dirs()],
        "log_paths": [os.path.join(path, "last-restore.log") for path in _stage_dirs()],
        "dism_log_paths": [os.path.join(path, "dism-restore.log") for path in _stage_dirs()],
        "post_deploy": post_deploy or {},
    }


def winpe_deploy_start(job_id: str, machine_id: str, image_filename: str, post_deploy: Optional[dict] = None):
    """Record a deployment handoff so the agent can report the WinPE result after reboot."""
    state = _winpe_deploy_state(job_id, machine_id, image_filename, post_deploy)
    os.makedirs(os.path.dirname(WINPE_PENDING_FILE), exist_ok=True)
    with open(WINPE_PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _clear_local_sysprep_artifacts(progress_cb: Optional[Callable] = None):
    """Remove stale Bretter-IMG sysprep notifications/tasks before a deploy."""
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    for path in (
        os.path.join(programdata, "BretterIMG", "sysprep-status.txt"),
        os.path.join(programdata, "BretterIMG", "agent-notifications.txt"),
        os.path.join(_system_drive() + "\\", "BretterIMG-Status.txt"),
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.info("Could not remove stale notification file %s: %s", path, exc)

    if os.name != "nt":
        return
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "Get-ScheduledTask -TaskName 'BretterIMG-Sysprep-*' | "
        "Unregister-ScheduledTask -Confirm:$false"
    )
    try:
        _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=60)
        if progress_cb:
            progress_cb("Cleared stale Bretter-IMG sysprep notifications before deploy")
    except Exception as exc:
        log.info("Could not clear stale Bretter-IMG sysprep scheduled tasks before deploy: %s", exc)


def remove_winpe_environment(progress_cb: Optional[Callable] = None):
    """Remove the managed WinPE boot entry and files after a successful deployment."""
    boot_id = _find_winpe_boot_id_optional(progress_cb)
    if boot_id:
        _run(["bcdedit", "/delete", boot_id, "/f"], timeout=60)
        if progress_cb:
            progress_cb(f"Removed WinPE boot entry {boot_id}")
    for stage_dir in _stage_dirs():
        shutil.rmtree(stage_dir, ignore_errors=True)
    if progress_cb:
        progress_cb("Removed managed WinPE staging files")


def capture_image(name: str, source_drive: str = "C:\\", method: str = "vss",
                  job_id: str = "", machine_id: str = "",
                  direct_capture_credentials: Optional[dict] = None,
                  sysprep_before_capture: bool = False,
                  unattended_file_path: str = "",
                  progress_cb: Optional[Callable] = None) -> str:
    """
    Capture the OS volume to a WIM file.
    method="vss"   — VSS shadow copy, no reboot (recommended)
    method="winpe" — Raises WinPERebootPending; agent resumes after reboot
    """
    if method == "winpe":
        winpe_capture_start(
            name, job_id, machine_id, source_drive, progress_cb, direct_capture_credentials,
            sysprep_before_capture, unattended_file_path,
        )
        # winpe_capture_start always raises WinPERebootPending — never returns
    # Default: VSS
    if progress_cb:
        progress_cb(f"Starting VSS-based capture of {source_drive}")
    try:
        return _vss_capture(name, source_drive, progress_cb)
    except Exception as e:
        log.error(f"VSS capture failed: {e}")
        raise RuntimeError(
            f"Capture failed: {e}\n\n"
            "Tip: Ensure the Volume Shadow Copy service is running:\n"
            "  net start VSS\n"
            "Or run: services.msc → Volume Shadow Copy → Start"
        )


def push_winpe_environment(progress_cb: Optional[Callable] = None):
    """
    Stage a local WinPE ramdisk boot environment for this machine.
    Downloads boot.wim/boot.sdi from the server, injects the Bretter-IMG
    launcher into boot.wim, and creates or updates the local BCD entry.
    """
    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)
    _write_winpe_entry_script()

    capture_script = os.path.join(WINPE_STAGE_DIR, "capture.bat")
    if not os.path.exists(capture_script):
        _write_to_stage_dirs(
            "capture.bat",
            "@echo off\necho Bretter-IMG WinPE Capture - waiting for a queued capture job script.\nwpeutil reboot\n",
        )

    boot_wim_path = _copy_winpe_assets_from_server(progress_cb)
    driver_count = _copy_winpe_drivers_from_server(progress_cb)
    if os.path.exists(WINPE_BACKGROUND_IMAGE):
        try:
            os.remove(WINPE_BACKGROUND_IMAGE)
            if progress_cb:
                progress_cb("Removed stale local WinPE background file")
        except Exception as exc:
            log.warning("Could not remove stale WinPE background file: %s", exc)
    if progress_cb:
        progress_cb(f"WinPE driver files staged: {driver_count}")
    try:
        _customize_winpe_boot_wim(progress_cb, boot_wim_path=boot_wim_path)
    except Exception as exc:
        detail = str(exc)
        if "access is denied" not in detail.lower() and "0x80070005" not in detail.lower():
            raise
        if progress_cb:
            progress_cb(f"WinPE boot.wim mount was denied; retrying with a fresh boot WIM ({detail})")
        fresh_boot_wim = _versioned_winpe_boot_wim()
        from transfer import download_winpe_asset
        download_winpe_asset("boot.wim", fresh_boot_wim, progress_cb=progress_cb)
        _set_active_winpe_boot_wim(fresh_boot_wim)
        _customize_winpe_boot_wim(progress_cb, boot_wim_path=fresh_boot_wim)
        boot_wim_path = fresh_boot_wim
    boot_id = _create_or_update_winpe_bcd_entry(progress_cb, boot_wim_path=boot_wim_path)

    if progress_cb:
        progress_cb(f"WinPE environment files staged in: {WINPE_STAGE_DIR}")
        progress_cb(f"WinPE boot entry is ready: {boot_id}")


def deploy_image(
    image_filename: str,
    target_drive: str = "C:\\",
    smb_credentials: Optional[dict] = None,
    job_id: str = "",
    machine_id: str = "",
    post_deploy: Optional[dict] = None,
    progress_cb: Optional[Callable] = None,
):
    """
    Stage a WinPE auto-restore script then reboot into WinPE.
    WinPE applies the image offline and reboots into the new OS.
    """
    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)
    _clear_local_sysprep_artifacts(progress_cb)

    safe_wim_filename = os.path.basename(image_filename).replace('"', "'")
    if not safe_wim_filename or safe_wim_filename != image_filename:
        raise RuntimeError("Invalid deployment image filename")
    config = _winpe_config()
    image_share = _batch_value(config.get("direct_capture_share", ""))
    if not image_share.startswith("\\\\"):
        raise RuntimeError(
            "Configure the image SMB share in WinPE Assets before deploying an image."
        )
    image_drive = _batch_value(config.get("direct_capture_drive") or "Z:")
    if len(image_drive) == 1:
        image_drive += ":"
    smb_credentials = smb_credentials or {}
    image_username = _batch_value(
        smb_credentials.get("username") or config.get("direct_capture_username", "")
    )
    image_password = _batch_value(
        smb_credentials.get("password") or config.get("direct_capture_password", "")
    )
    if not image_username:
        raise RuntimeError("No SMB credential is available for the deployment image share")
    fallback_target = target_drive.rstrip("\\/")
    server_url = _batch_value(SERVER_URL.rstrip("/"))
    agent_token = _batch_value(_current_agent_token())
    if job_id:
        winpe_deploy_start(job_id, machine_id, safe_wim_filename, post_deploy)
    deploy_state = _winpe_deploy_state(job_id, machine_id, safe_wim_filename, post_deploy)
    deploy_state_line = _batch_echo_literal(json.dumps(deploy_state, separators=(",", ":")))
    restore_content = f"""@echo off
setlocal DisableDelayedExpansion
echo Bretter-IMG: Preparing SMB image restore...
set RUN_DRIVE=%~d0
set "STAGE_DIR=%~dp0"
if not defined RESTORE_SOURCE_STAGE set "RESTORE_SOURCE_STAGE=%STAGE_DIR%"
if /I not "%RUN_DRIVE%"=="X:" if not "%RESTORE_RUNNING_FROM_RAM%"=="1" (
  if not exist "X:\\BretterIMG" mkdir "X:\\BretterIMG"
  if not exist "X:\\BretterIMG\\stage" mkdir "X:\\BretterIMG\\stage"
  if exist "%STAGE_DIR%drivers" xcopy /E /I /Y /Q "%STAGE_DIR%drivers" "X:\\BretterIMG\\stage\\drivers" >nul 2>&1
  set "RESTORE_SOURCE_STAGE=X:\\BretterIMG\\stage\\"
  copy /y "%~f0" "X:\\BretterIMG\\active-restore.bat" >nul 2>&1
  if exist "X:\\BretterIMG\\active-restore.bat" (
    set RESTORE_RUNNING_FROM_RAM=1
    call "X:\\BretterIMG\\active-restore.bat"
    exit /b
  )
)
set TARGET=
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do if not defined TARGET if exist "%%D:\\Windows\\System32\\config\\SYSTEM" set TARGET=%%D:
if not defined TARGET set TARGET={fallback_target}
set "RESTORE_DIR=%TARGET%\\ProgramData\\BretterIMG\\winpe"
set "ROOT_RESTORE_DIR=%TARGET%\\BretterIMG\\winpe"
set "RAM_RESTORE_DIR=X:\\BretterIMG\\restore"
set "DRIVER_STORE=%RESTORE_SOURCE_STAGE%drivers"
if not exist "%DRIVER_STORE%" set "DRIVER_STORE=X:\\BretterIMG\\drivers"
if not exist "%RAM_RESTORE_DIR%" mkdir "%RAM_RESTORE_DIR%"
set "RESTORE_LOG=%RAM_RESTORE_DIR%\\last-restore.log"
set "RESULT_FILE=%RESTORE_DIR%\\deploy-result.txt"
set "ROOT_RESULT_FILE=%ROOT_RESTORE_DIR%\\deploy-result.txt"
set "DISM_LOG=%RESTORE_DIR%\\dism-restore.log"
if not exist "%RESTORE_DIR%" mkdir "%RESTORE_DIR%"
if not exist "%ROOT_RESTORE_DIR%" mkdir "%ROOT_RESTORE_DIR%"
if exist "%RESULT_FILE%" del /f /q "%RESULT_FILE%" >nul 2>&1
if exist "%ROOT_RESULT_FILE%" del /f /q "%ROOT_RESULT_FILE%" >nul 2>&1
echo Restore target selected: %TARGET%\\ >> "%RESTORE_LOG%"
echo Volume list before deploy: >> "%RESTORE_LOG%"
set LV=X:\\BretterIMG-restore-listvol.txt
echo list volume > "%LV%"
diskpart /s "%LV%" >> "%RESTORE_LOG%" 2>&1
echo Target free space before deploy: >> "%RESTORE_LOG%"
fsutil volume diskfree %TARGET%\\ >> "%RESTORE_LOG%" 2>&1
set "IMAGE_SHARE={image_share}"
set "IMAGE_USER={image_username}"
set "IMAGE_PASS={image_password}"
set "SERVER_URL={server_url}"
set "AGENT_TOKEN={agent_token}"
set "JOB_ID={_batch_value(job_id)}"
set "IMAGE_HOST=%IMAGE_SHARE:~2%"
for /f "tokens=1 delims=\\" %%S in ("%IMAGE_HOST%") do set "IMAGE_HOST=%%S"
set "IMAGE_SHARE_LEAF=%IMAGE_SHARE:~2%"
for /f "tokens=2 delims=\\" %%S in ("%IMAGE_SHARE_LEAF%") do set "IMAGE_SHARE_LEAF=%%S"
set IMAGE_NETBIOS=
set IMAGE_ALT_SHARE=
net use {image_drive} /delete /y >nul 2>&1
echo Initializing WinPE networking for %IMAGE_HOST%...
echo Loading WinPE network and storage drivers from %DRIVER_STORE%... >> "%RESTORE_LOG%"
if exist "%DRIVER_STORE%" (
    for /r "%DRIVER_STORE%" %%I in (*.inf) do drvload "%%I" >> "%RESTORE_LOG%" 2>&1
)
wpeutil InitializeNetwork >> "%RESTORE_LOG%" 2>&1
netcfg -winpe >> "%RESTORE_LOG%" 2>&1
net start dhcp >> "%RESTORE_LOG%" 2>&1
net start lmhosts >> "%RESTORE_LOG%" 2>&1
net start lanmanworkstation >> "%RESTORE_LOG%" 2>&1
ipconfig /renew >> "%RESTORE_LOG%" 2>&1
ipconfig
ipconfig /all >> "%RESTORE_LOG%" 2>&1
for /L %%R in (1,1,12) do (
    ping -n 1 -w 3000 "%IMAGE_HOST%" >nul 2>&1 && goto IMAGE_HOST_READY
    echo Waiting for SMB host %IMAGE_HOST% attempt %%R of 12...
    echo Waiting for SMB host %IMAGE_HOST% attempt %%R of 12... >> "%RESTORE_LOG%"
    ping -n 6 127.0.0.1 >nul
)
:IMAGE_HOST_READY
echo Mapping image share %IMAGE_SHARE% to {image_drive}...
call :IMAGE_MAP_WITH_RETRY
if "%MAP_RC%"=="0" goto IMAGE_MAPPED
echo ERROR: Could not map image share %IMAGE_SHARE%. NET USE error %MAP_RC%.
echo ERROR: Could not map image share %IMAGE_SHARE%. NET USE error %MAP_RC%. >> "%RESTORE_LOG%"
net use >> "%RESTORE_LOG%" 2>&1
> "%RESULT_FILE%" echo failed: SMB mapping failed with NET USE error %MAP_RC%
> "%ROOT_RESULT_FILE%" echo failed: SMB mapping failed with NET USE error %MAP_RC%
call :COPY_RESTORE_LOG
wpeutil reboot
exit /b %MAP_RC%
:IMAGE_MAPPED
set "WIM={image_drive}\\{safe_wim_filename}"
if not exist "%WIM%" (
    > "%RESULT_FILE%" echo failed: Selected image not found at %WIM%
    > "%ROOT_RESULT_FILE%" echo failed: Selected image not found at %WIM%
  echo ERROR: Selected image not found: %WIM% >> "%RESTORE_LOG%"
  net use {image_drive} /delete /y >nul 2>&1
  call :COPY_RESTORE_LOG
  wpeutil reboot
  exit /b 1
)
echo SMB image found: "%WIM%"
echo SMB image found: "%WIM%" >> "%RESTORE_LOG%"
for %%A in ("%WIM%") do echo SMB image size: %%~zA bytes >> "%RESTORE_LOG%"
call :FORMAT_TARGET_VOLUME
if errorlevel 1 (
    echo ERROR: Could not format target volume %TARGET% before applying image. >> "%RESTORE_LOG%"
    > "%RESULT_FILE%" echo failed: Could not format target volume %TARGET%
    > "%ROOT_RESULT_FILE%" echo failed: Could not format target volume %TARGET%
    net use {image_drive} /delete /y >nul 2>&1
    call :COPY_RESTORE_LOG
    wpeutil reboot
    exit /b 1
)
set "SCRATCH=X:\\BretterIMG\\scratch"
if not exist "%SCRATCH%" mkdir "%SCRATCH%"
echo Applying "%WIM%" to %TARGET%\\. This can take several minutes.
echo Applying "%WIM%" to %TARGET%\\. >> "%RESTORE_LOG%"
call :RUN_DISM_APPLY
if not exist "%RESTORE_DIR%" mkdir "%RESTORE_DIR%"
if not exist "%ROOT_RESTORE_DIR%" mkdir "%ROOT_RESTORE_DIR%"
echo DISM Apply-Image exited with code %DISM_RC%. >> "%RESTORE_LOG%"
net use {image_drive} /delete /y >nul 2>&1
if not "%DISM_RC%"=="0" (
    echo ERROR: DISM failed code %DISM_RC% >> "%RESTORE_LOG%"
    > "%RESULT_FILE%" echo failed: DISM Apply-Image returned %DISM_RC%
    > "%ROOT_RESULT_FILE%" echo failed: DISM Apply-Image returned %DISM_RC%
    call :COPY_RESTORE_LOG
    wpeutil reboot
    exit /b %DISM_RC%
)
call :INJECT_APPLIED_OS_DRIVERS
call :REBUILD_WINDOWS_BOOT
if errorlevel 1 (
    echo ERROR: Could not rebuild Windows boot files. >> "%RESTORE_LOG%"
    > "%RESULT_FILE%" echo failed: Could not rebuild Windows boot files
    > "%ROOT_RESULT_FILE%" echo failed: Could not rebuild Windows boot files
    call :COPY_RESTORE_LOG
    wpeutil reboot
    exit /b 1
)
echo Image applied. Rebooting...
> "%RESULT_FILE%" echo completed: Image applied successfully
> "%ROOT_RESULT_FILE%" echo completed: Image applied successfully
call :REPORT_WINPE_PROGRESS "Image applied successfully"
call :CLEAN_APPLIED_OS_ARTIFACTS
call :WRITE_DEPLOY_PENDING
echo Deployment result written to %RESULT_FILE% and %ROOT_RESULT_FILE%. >> "%RESTORE_LOG%"
call :COPY_RESTORE_LOG
wpeutil reboot
exit /b 0

:RUN_DISM_APPLY
set DISM_RC=1
set "DISM_RUNNER=X:\\BretterIMG-run-dism.bat"
> "%DISM_RUNNER%" echo @echo off
>> "%DISM_RUNNER%" echo dism /Apply-Image /ImageFile:"%WIM%" /Index:1 /ApplyDir:%TARGET%\\ /CheckIntegrity /ScratchDir:"%SCRATCH%" /LogPath:"%DISM_LOG%" /LogLevel:4
>> "%DISM_RUNNER%" echo echo __DISM_EXIT__=%%errorlevel%%
for /f "delims=" %%L in ('call "%DISM_RUNNER%" 2^>^&1') do (
  call :HANDLE_DISM_LINE "%%L"
)
exit /b %DISM_RC%

:HANDLE_DISM_LINE
set "DISM_LINE=%~1"
if "%DISM_LINE:~0,14%"=="__DISM_EXIT__=" (
  set "DISM_RC=%DISM_LINE:__DISM_EXIT__=%"
  exit /b 0
)
echo(%DISM_LINE% | findstr /c:"%%" >nul 2>&1
if errorlevel 1 (
  echo(%DISM_LINE%
  echo(%DISM_LINE% >> "%RESTORE_LOG%"
  exit /b 0
)
echo IMAGE APPLY PROGRESS: %DISM_LINE%
echo IMAGE APPLY PROGRESS: %DISM_LINE% >> "%RESTORE_LOG%"
call :SHOW_DISM_PROGRESS "%DISM_LINE%"
call :REPORT_WINPE_PROGRESS "WinPE image apply progress: %DISM_LINE%"
exit /b 0

:SHOW_DISM_PROGRESS
set "DISM_PROGRESS_LINE=%~1"
setlocal EnableDelayedExpansion
set "DISM_PCT="
for %%P in (!DISM_PROGRESS_LINE!) do (
  echo(%%P | findstr /r "[0-9][0-9]*.*%%" >nul 2>&1
  if not errorlevel 1 if not defined DISM_PCT set "DISM_PCT=%%P"
)
if defined DISM_PCT (
  set "DISM_PCT=!DISM_PCT:%%=!"
  for /f "tokens=1 delims=." %%P in ("!DISM_PCT!") do set "DISM_PCT=%%P"
)
if defined DISM_PCT (
  echo Bretter-IMG image apply progress: !DISM_PCT!%%
  echo Bretter-IMG image apply progress: !DISM_PCT!%% >> "%RESTORE_LOG%"
)
endlocal
exit /b 0

:INJECT_APPLIED_OS_DRIVERS
if not exist "%DRIVER_STORE%" (
    echo No staged driver store found for applied OS injection. >> "%RESTORE_LOG%"
    exit /b 0
)
echo Injecting staged drivers into applied Windows image from %DRIVER_STORE%... >> "%RESTORE_LOG%"
dism /Image:%TARGET%\\ /Add-Driver /Driver:"%DRIVER_STORE%" /Recurse /LogPath:"%RESTORE_DIR%\\dism-driver-inject.log" /LogLevel:4 >> "%RESTORE_LOG%" 2>&1
set DRIVER_INJECT_RC=%errorlevel%
if not "%DRIVER_INJECT_RC%"=="0" (
    echo WARNING: Applied OS driver injection returned %DRIVER_INJECT_RC%; continuing with boot rebuild. >> "%RESTORE_LOG%"
)
exit /b 0

:REBUILD_WINDOWS_BOOT
echo Rebuilding Windows boot files for %TARGET%\\Windows... >> "%RESTORE_LOG%"
if not exist "%TARGET%\\Windows\\System32\\winload.efi" (
    echo WARNING: winload.efi was not found at %TARGET%\\Windows\\System32\\winload.efi before bcdboot. >> "%RESTORE_LOG%"
)
set "EFI_DRIVE=S:"
call :ASSIGN_EFI_SYSTEM_PARTITION
if defined EFI_READY (
    echo Running bcdboot %TARGET%\\Windows /s %EFI_DRIVE% /f UEFI >> "%RESTORE_LOG%"
    bcdboot "%TARGET%\\Windows" /s %EFI_DRIVE% /f UEFI >> "%RESTORE_LOG%" 2>&1
    set BOOT_RC=%errorlevel%
    if "%BOOT_RC%"=="0" exit /b 0
    echo UEFI bcdboot failed with %BOOT_RC%; trying firmware-neutral bcdboot. >> "%RESTORE_LOG%"
    bcdboot "%TARGET%\\Windows" /s %EFI_DRIVE% /f ALL >> "%RESTORE_LOG%" 2>&1
    set BOOT_RC=%errorlevel%
    if "%BOOT_RC%"=="0" exit /b 0
)
echo Could not identify an EFI system partition; trying bcdboot without explicit /s. >> "%RESTORE_LOG%"
bcdboot "%TARGET%\\Windows" /f ALL >> "%RESTORE_LOG%" 2>&1
set BOOT_RC=%errorlevel%
exit /b %BOOT_RC%

:ASSIGN_EFI_SYSTEM_PARTITION
set EFI_READY=
set EFI_VOL=
set EFI_LIST=X:\\BretterIMG-list-efi.txt
set EFI_DP=X:\\BretterIMG-assign-efi.txt
echo list volume > "%EFI_DP%"
diskpart /s "%EFI_DP%" > "%EFI_LIST%" 2>&1
type "%EFI_LIST%" >> "%RESTORE_LOG%"
for /f "tokens=2" %%V in ('findstr /i "FAT32" "%EFI_LIST%" ^| findstr /i "System"') do if not defined EFI_VOL set "EFI_VOL=%%V"
if not defined EFI_VOL for /f "tokens=2" %%V in ('findstr /i "FAT32" "%EFI_LIST%"') do if not defined EFI_VOL set "EFI_VOL=%%V"
if not defined EFI_VOL (
    echo No FAT32 EFI volume found. >> "%RESTORE_LOG%"
    exit /b 1
)
echo select volume %EFI_VOL% > "%EFI_DP%"
echo assign letter=%EFI_DRIVE:~0,1% noerr >> "%EFI_DP%"
echo exit >> "%EFI_DP%"
diskpart /s "%EFI_DP%" >> "%RESTORE_LOG%" 2>&1
if errorlevel 1 exit /b 1
set EFI_READY=1
echo EFI system partition volume %EFI_VOL% assigned to %EFI_DRIVE%. >> "%RESTORE_LOG%"
exit /b 0

:REPORT_WINPE_PROGRESS
if not defined JOB_ID exit /b 0
if not defined SERVER_URL exit /b 0
if not defined AGENT_TOKEN exit /b 0
where curl.exe >nul 2>&1
if errorlevel 1 exit /b 0
set "PROGRESS_JSON=X:\\BretterIMG-progress.json"
> "%PROGRESS_JSON%" echo {{"status":"running","log":"%~1"}}
curl.exe -k -s -m 5 -X PATCH "%SERVER_URL%/api/jobs/%JOB_ID%" -H "Authorization: Bearer %AGENT_TOKEN%" -H "Content-Type: application/json" --data-binary "@%PROGRESS_JSON%" >nul 2>&1
exit /b 0

:FORMAT_TARGET_VOLUME
if /I "%TARGET%"=="X:" exit /b 1
if /I "%TARGET%"=="{image_drive}" exit /b 1
echo Formatting target volume %TARGET% before image apply. >> "%RESTORE_LOG%"
set DP=X:\\BretterIMG-format-target.txt
echo select volume %TARGET:~0,1% > "%DP%"
echo format fs=ntfs quick label=Windows override >> "%DP%"
echo assign letter=%TARGET:~0,1% >> "%DP%"
echo exit >> "%DP%"
diskpart /s "%DP%" >> "%RESTORE_LOG%" 2>&1
set FORMAT_RC=%errorlevel%
if not "%FORMAT_RC%"=="0" exit /b %FORMAT_RC%
if not exist "%RESTORE_DIR%" mkdir "%RESTORE_DIR%"
if not exist "%ROOT_RESTORE_DIR%" mkdir "%ROOT_RESTORE_DIR%"
exit /b 0

:COPY_RESTORE_LOG
if not exist "%RESTORE_DIR%" mkdir "%RESTORE_DIR%" >nul 2>&1
if not exist "%ROOT_RESTORE_DIR%" mkdir "%ROOT_RESTORE_DIR%" >nul 2>&1
if exist "%RESTORE_LOG%" copy /y "%RESTORE_LOG%" "%RESTORE_DIR%\\last-restore.log" >nul 2>&1
if exist "%RESTORE_LOG%" copy /y "%RESTORE_LOG%" "%ROOT_RESTORE_DIR%\\last-restore.log" >nul 2>&1
exit /b 0

:CLEAN_APPLIED_OS_ARTIFACTS
if exist "%TARGET%\\ProgramData\\BretterIMG\\winpe_pending.json" del /f /q "%TARGET%\\ProgramData\\BretterIMG\\winpe_pending.json" >nul 2>&1
if exist "%TARGET%\\ProgramData\\BretterIMG\\post_deploy_pending.json" del /f /q "%TARGET%\\ProgramData\\BretterIMG\\post_deploy_pending.json" >nul 2>&1
if exist "%TARGET%\\ProgramData\\BretterIMG\\sysprep-status.txt" del /f /q "%TARGET%\\ProgramData\\BretterIMG\\sysprep-status.txt" >nul 2>&1
if exist "%TARGET%\\ProgramData\\BretterIMG\\agent-notifications.txt" del /f /q "%TARGET%\\ProgramData\\BretterIMG\\agent-notifications.txt" >nul 2>&1
if exist "%TARGET%\\BretterIMG-Status.txt" del /f /q "%TARGET%\\BretterIMG-Status.txt" >nul 2>&1
if exist "%TARGET%\\ProgramData\\BretterIMG\\sysprep" rmdir /s /q "%TARGET%\\ProgramData\\BretterIMG\\sysprep" >nul 2>&1
for %%T in ("%TARGET%\\Windows\\System32\\Tasks\\BretterIMG-Sysprep*") do if exist "%%~fT" del /f /q "%%~fT" >nul 2>&1
echo Skipped automatic agent injection into applied OS. >> "%RESTORE_LOG%"
exit /b 0

:WRITE_DEPLOY_PENDING
set "PENDING_FILE=%TARGET%\\ProgramData\\BretterIMG\\winpe_pending.json"
if not exist "%TARGET%\\ProgramData\\BretterIMG" mkdir "%TARGET%\\ProgramData\\BretterIMG" >nul 2>&1
> "%PENDING_FILE%" echo {deploy_state_line}
if errorlevel 1 echo WARNING: Could not write deploy pending state to %PENDING_FILE%. >> "%RESTORE_LOG%"
if not errorlevel 1 echo Deploy pending state written to %PENDING_FILE%. >> "%RESTORE_LOG%"
exit /b 0

:IMAGE_MAP_WITH_RETRY
set MAP_RC=1
set IMAGE_MAP_ATTEMPT=0
:IMAGE_MAP_RETRY
set /a IMAGE_MAP_ATTEMPT+=1
echo SMB map attempt %IMAGE_MAP_ATTEMPT% of 18 for %IMAGE_SHARE% as %IMAGE_USER%...
echo SMB map attempt %IMAGE_MAP_ATTEMPT% of 18 for %IMAGE_SHARE% as %IMAGE_USER%... >> "%RESTORE_LOG%"
call :IMAGE_TRY_MAP "%IMAGE_SHARE%" "%IMAGE_USER%"
if not errorlevel 1 exit /b 0
call :IMAGE_TRY_MAP "%IMAGE_SHARE%" "%IMAGE_HOST%\\%IMAGE_USER%"
if not errorlevel 1 exit /b 0
if "%IMAGE_MAP_ATTEMPT%"=="1" call :IMAGE_SMB_DIAGNOSTICS
if defined IMAGE_ALT_SHARE (
    call :IMAGE_TRY_MAP "%IMAGE_ALT_SHARE%" "%IMAGE_USER%"
    if not errorlevel 1 exit /b 0
    call :IMAGE_TRY_MAP "%IMAGE_ALT_SHARE%" "%IMAGE_NETBIOS%\\%IMAGE_USER%"
    if not errorlevel 1 exit /b 0
)
if %IMAGE_MAP_ATTEMPT% LSS 18 (
    ping -n 11 127.0.0.1 >nul
    goto IMAGE_MAP_RETRY
)
exit /b %MAP_RC%

:IMAGE_TRY_MAP
net use {image_drive} /delete /y >nul 2>&1
net use {image_drive} "%~1" "%IMAGE_PASS%" /user:"%~2" /persistent:no >> "%RESTORE_LOG%" 2>&1
set MAP_RC=%errorlevel%
exit /b %MAP_RC%

:IMAGE_SMB_DIAGNOSTICS
route print >> "%RESTORE_LOG%" 2>&1
arp -a >> "%RESTORE_LOG%" 2>&1
nbtstat -A "%IMAGE_HOST%" >> "%RESTORE_LOG%" 2>&1
for /f "tokens=1" %%N in ('nbtstat -A "%IMAGE_HOST%" ^| find "<20>"') do if not defined IMAGE_NETBIOS set "IMAGE_NETBIOS=%%N"
if defined IMAGE_NETBIOS set "IMAGE_ALT_SHARE=\\\\%IMAGE_NETBIOS%\\%IMAGE_SHARE_LEAF%"
net view "\\\\%IMAGE_HOST%" >> "%RESTORE_LOG%" 2>&1
if defined IMAGE_NETBIOS net view "\\\\%IMAGE_NETBIOS%" >> "%RESTORE_LOG%" 2>&1
exit /b 0
"""
    restore_paths = _write_to_stage_dirs("restore.bat", restore_content)
    _write_to_stage_dirs("winpe-operation.txt", "restore\n")

    if progress_cb:
        progress_cb(f"WinPE restore script staged at {', '.join(restore_paths)}")
        progress_cb("Embedding restore script into boot.wim as a fallback...")
    _customize_winpe_boot_wim(progress_cb, {
        "winpe-operation.txt": "restore\n",
        "restore-embedded.bat": restore_content,
    })

    if progress_cb:
        progress_cb("Preparing one-time WinPE boot for deploy...")

    _schedule_winpe_reboot(
        "Bretter-IMG: Rebooting into WinPE to apply image",
        10,
        progress_cb,
        screen_message="Bretter-IMG: Rebooting into WinPE to deploy image...",
    )
    raise WinPERebootPending("Machine rebooting into WinPE to apply the selected image from SMB")


def run_wipe(drive_number: int = 0, progress_cb: Optional[Callable] = None):
    """
    Secure wipe using diskpart clean all.
    WARNING: Irreversible — destroys all data on the drive.
    """
    script = os.path.join(TEMP_DIR, "wipe.txt")
    os.makedirs(TEMP_DIR, exist_ok=True)
    with open(script, "w") as f:
        f.write(f"select disk {drive_number}\nclean all\n")

    if progress_cb:
        progress_cb(f"Starting secure wipe of disk {drive_number} — this may take hours")

    _run(["diskpart", "/s", script], progress_cb=progress_cb, timeout=86400)

    if progress_cb:
        progress_cb("Wipe complete")
