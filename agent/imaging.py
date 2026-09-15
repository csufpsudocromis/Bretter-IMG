"""
Imaging operations using Windows DISM and WinPE.

Capture:  uses DISM to create a WIM from the live C: drive.
Deploy:   writes a WinPE auto-script and reboots into WinPE which applies the WIM.
Wipe:     overwrites drive with zeros using diskpart + format.
"""

import subprocess
import os
import logging
from typing import Callable, Optional

from config import TEMP_DIR, WINPE_STAGE_DIR

log = logging.getLogger("imaging")


def _run(cmd: list, progress_cb: Optional[Callable] = None) -> str:
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if progress_cb and result.stdout:
        for line in result.stdout.splitlines():
            if line.strip():
                progress_cb(line)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (rc={result.returncode}): {result.stderr[:500]}")
    return result.stdout


def capture_image(name: str, source_drive: str = "C:\\", progress_cb: Optional[Callable] = None) -> str:
    """
    Capture the live OS volume to a WIM file using DISM.
    Returns the path to the created WIM.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    safe_name = name.replace(" ", "_").replace("/", "-")
    wim_path = os.path.join(TEMP_DIR, f"{safe_name}.wim")

    if progress_cb:
        progress_cb(f"Starting DISM capture of {source_drive} → {wim_path}")

    _run([
        "dism",
        f"/Capture-Image",
        f"/ImageFile:{wim_path}",
        f"/CaptureDir:{source_drive}",
        f"/Name:{name}",
        "/Compress:fast",
        "/CheckIntegrity",
        "/Verify",
    ], progress_cb=progress_cb)

    return wim_path


def deploy_image(wim_path: str, target_drive: str = "C:\\", progress_cb: Optional[Callable] = None):
    """
    Stage a WinPE auto-restore script, then reboot into WinPE.
    WinPE will apply the image and reboot into the freshly imaged OS.
    """
    os.makedirs(WINPE_STAGE_DIR, exist_ok=True)

    # Write the restore script that WinPE will auto-run
    restore_script = os.path.join(WINPE_STAGE_DIR, "restore.bat")
    with open(restore_script, "w") as f:
        f.write(f"""@echo off
echo Bretter-IMG: Applying image...
dism /Apply-Image /ImageFile:"{wim_path}" /Index:1 /ApplyDir:{target_drive} /CheckIntegrity
if %errorlevel% neq 0 (
    echo ERROR: DISM failed with code %errorlevel%
    pause
    exit /b %errorlevel%
)
echo Image applied successfully. Rebooting...
wpeutil reboot
""")

    # Write winpeshl.ini to auto-run restore script on WinPE boot
    winpeshl = os.path.join(WINPE_STAGE_DIR, "winpeshl.ini")
    with open(winpeshl, "w") as f:
        f.write(f"""[LaunchApps]
"{restore_script}"
""")

    if progress_cb:
        progress_cb(f"WinPE restore script staged at {restore_script}")
        progress_cb("Rebooting into WinPE in 10 seconds...")

    # Reboot into WinPE (BCDEdit entry must pre-exist from build_winpe.ps1)
    subprocess.Popen(["shutdown", "/r", "/t", "10", "/c", "Bretter-IMG: Rebooting into WinPE to apply image"])


def run_wipe(drive_number: int = 0, progress_cb: Optional[Callable] = None):
    """
    Secure wipe using diskpart clean all (writes zeros to every sector).
    WARNING: This is destructive and irreversible.
    """
    script = os.path.join(TEMP_DIR, "wipe.txt")
    os.makedirs(TEMP_DIR, exist_ok=True)
    with open(script, "w") as f:
        f.write(f"select disk {drive_number}\nclean all\n")

    if progress_cb:
        progress_cb(f"Starting secure wipe of disk {drive_number} — this may take hours")

    _run(["diskpart", "/s", script], progress_cb=progress_cb)

    if progress_cb:
        progress_cb("Wipe complete")
