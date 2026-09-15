import os
import shutil
import json
import hashlib
import zipfile
import tempfile
import subprocess
from typing import Dict

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..core.config import settings
from ..core.security import get_current_agent_machine_id, get_current_user

router = APIRouter(prefix="/api/winpe", tags=["winpe"])

REQUIRED_WINPE_ASSET_FILES = {"boot.wim", "boot.sdi"}
REQUIRED_BOOTABLE_ISO_ASSET_FILES = {"boot.wim", "boot.sdi", "efisys.bin"}
OPTIONAL_BOOTABLE_ISO_ASSET_FILES = {"etfsboot.com"}
WINPE_ASSET_FILES = REQUIRED_WINPE_ASSET_FILES | REQUIRED_BOOTABLE_ISO_ASSET_FILES | OPTIONAL_BOOTABLE_ISO_ASSET_FILES
DRIVER_EXTENSIONS = {".inf", ".sys", ".cat", ".dll", ".cab"}
BOOT_IMAGE_WIM_PATHS = {
    "efisys.bin": [
        "/Windows/Boot/DVD/EFI/en-US/efisys_noprompt.bin",
        "/Windows/Boot/DVD/EFI/en-US/efisys.bin",
    ],
    "etfsboot.com": [
        "/Windows/Boot/DVD/PCAT/etfsboot.com",
    ],
}
DEFAULT_CONFIG = {
    "boot_description": "Bretter-IMG WinPE",
    "direct_capture_enabled": "false",
    "direct_capture_share": "",
    "direct_capture_username": "",
    "direct_capture_password": "",
    "direct_capture_drive": "Z:",
}


class WinPEConfigUpdate(BaseModel):
    boot_description: str
    direct_capture_enabled: bool = False
    direct_capture_share: str = ""
    direct_capture_username: str = ""
    direct_capture_password: str = ""
    direct_capture_drive: str = "Z:"


class BootableUsbPackageRequest(BaseModel):
    server_url: str = ""
    image_share: str = ""
    smb_username: str = ""
    smb_password: str = ""
    map_drive: str = "Z:"


def _asset_path(filename: str) -> str:
    if filename not in WINPE_ASSET_FILES:
        raise HTTPException(status_code=404, detail="WinPE asset not supported")
    return os.path.join(settings.WINPE_STORE_PATH, filename)


def _config_path() -> str:
    return os.path.join(settings.WINPE_STORE_PATH, "winpe-config.json")


def _drivers_dir() -> str:
    return os.path.join(settings.WINPE_STORE_PATH, "drivers")


def _driver_path(filename: str) -> str:
    safe = os.path.basename(filename)
    if not safe or safe != filename:
        raise HTTPException(status_code=400, detail="Invalid driver filename")
    return os.path.join(_drivers_dir(), safe)


def _read_config() -> Dict[str, str]:
    path = _config_path()
    if not os.path.exists(path):
        return DEFAULT_CONFIG.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()
    config = DEFAULT_CONFIG.copy()
    config.update({k: v for k, v in data.items() if isinstance(v, str)})
    return config


def _write_config(config: Dict[str, str]) -> Dict[str, str]:
    os.makedirs(settings.WINPE_STORE_PATH, exist_ok=True)
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return config


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_info(path: str, filename: str) -> Dict[str, object]:
    exists = os.path.exists(path)
    derivable = False if exists else _asset_derivable_from_boot_wim(filename)
    return {
        "filename": filename,
        "required": filename in REQUIRED_WINPE_ASSET_FILES,
        "iso_required": filename in REQUIRED_BOOTABLE_ISO_ASSET_FILES,
        "iso_optional": filename in OPTIONAL_BOOTABLE_ISO_ASSET_FILES,
        "available": exists,
        "derivable_from_boot_wim": derivable,
        "size_bytes": os.path.getsize(path) if exists else 0,
        "updated_at": os.path.getmtime(path) if exists else None,
        "sha256": _file_hash(path) if exists else None,
    }


def _asset_info(filename: str) -> Dict[str, object]:
    path = _asset_path(filename)
    return _file_info(path, filename)


def _driver_files():
    path = _drivers_dir()
    if not os.path.isdir(path):
        return []
    return [
        _file_info(os.path.join(path, filename), filename)
        for filename in sorted(os.listdir(path))
        if os.path.isfile(os.path.join(path, filename))
    ]


def _walk_driver_paths():
    path = _drivers_dir()
    if not os.path.isdir(path):
        return []
    files = []
    for root, _, filenames in os.walk(path):
        for filename in filenames:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, path)
            files.append((full_path, rel_path))
    return files


def _wimlib_imagex_path() -> str | None:
    return shutil.which("wimlib-imagex")


def _iso_tool_status() -> Dict[str, object]:
    return {
        "wimlib_imagex": _wimlib_imagex_path(),
        "xorriso": shutil.which("xorriso"),
    }


def _wim_contains_path(internal_path: str) -> bool:
    wimlib_imagex = _wimlib_imagex_path()
    boot_wim = os.path.join(settings.WINPE_STORE_PATH, "boot.wim")
    if not wimlib_imagex or not os.path.exists(boot_wim):
        return False
    env = os.environ.copy()
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    result = subprocess.run(
        [wimlib_imagex, "dir", boot_wim, "1", f"--path={internal_path}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return result.returncode == 0


def _asset_derivable_from_boot_wim(filename: str) -> bool:
    return any(_wim_contains_path(path) for path in BOOT_IMAGE_WIM_PATHS.get(filename, []))


def _bootable_iso_asset_ready(filename: str) -> bool:
    return os.path.exists(_asset_path(filename)) or _asset_derivable_from_boot_wim(filename)


def _extract_boot_image_asset(filename: str, work_dir: str, required: bool = False) -> str | None:
    uploaded = _asset_path(filename)
    if os.path.exists(uploaded):
        return uploaded
    candidates = BOOT_IMAGE_WIM_PATHS.get(filename, [])
    if not candidates:
        if required:
            raise HTTPException(status_code=400, detail=f"Missing WinPE asset: {filename}")
        return None
    wimlib_imagex = _wimlib_imagex_path()
    if not wimlib_imagex:
        if required:
            raise HTTPException(
                status_code=500,
                detail=f"Cannot extract {filename} from boot.wim: install wimtools so wimlib-imagex is available.",
            )
        return None

    extract_dir = os.path.join(work_dir, f"extract-{filename}")
    os.makedirs(extract_dir, exist_ok=True)
    env = os.environ.copy()
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    for internal_path in candidates:
        result = subprocess.run(
            [
                wimlib_imagex,
                "extract",
                _asset_path("boot.wim"),
                "1",
                internal_path,
                f"--dest-dir={extract_dir}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode != 0:
            continue
        extracted = os.path.join(extract_dir, os.path.basename(internal_path))
        if os.path.exists(extracted):
            target = os.path.join(work_dir, filename)
            shutil.copy2(extracted, target)
            return target
    if required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing WinPE asset: {filename}. Upload it or use a boot.wim that contains Microsoft's DVD boot files.",
        )
    return None


def _extract_wim_file(internal_path: str, destination: str, work_dir: str, required: bool = True) -> str | None:
    wimlib_imagex = _wimlib_imagex_path()
    if not wimlib_imagex:
        if required:
            raise HTTPException(
                status_code=500,
                detail="Cannot extract boot files from boot.wim: install wimtools so wimlib-imagex is available.",
            )
        return None
    extract_dir = os.path.join(work_dir, "media-boot-files")
    os.makedirs(extract_dir, exist_ok=True)
    env = os.environ.copy()
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    result = subprocess.run(
        [
            wimlib_imagex,
            "extract",
            _asset_path("boot.wim"),
            "1",
            internal_path,
            f"--dest-dir={extract_dir}",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    extracted = os.path.join(extract_dir, os.path.basename(internal_path))
    if result.returncode == 0 and os.path.exists(extracted):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(extracted, destination)
        return destination
    if required:
        detail = (result.stderr or result.stdout or f"{internal_path} was not found in boot.wim").strip()
        raise HTTPException(status_code=400, detail=f"Could not extract {internal_path}: {detail[-800:]}")
    return None


def _is_driver_file(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in DRIVER_EXTENSIONS


def _normalize_drive(value: str) -> str:
    drive = value.strip().upper() or "Z:"
    if len(drive) == 1:
        drive = f"{drive}:"
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise HTTPException(status_code=400, detail="Map drive must look like Z:")
    return drive


def _bootable_usb_config(body: BootableUsbPackageRequest) -> Dict[str, str]:
    current = _read_config()
    share = body.image_share.strip() or current.get("direct_capture_share", "").strip()
    if not share.startswith("\\\\"):
        raise HTTPException(status_code=400, detail="Image share must be a UNC path like \\\\server\\share")
    server_url = body.server_url.strip()
    return {
        "server_url": server_url,
        "image_share": share,
        "smb_username": body.smb_username.strip() or current.get("direct_capture_username", "").strip(),
        "smb_password": body.smb_password or current.get("direct_capture_password", ""),
        "map_drive": _normalize_drive(body.map_drive or current.get("direct_capture_drive", "Z:")),
    }


def _batch_literal(value: str) -> str:
    text = str(value or "")
    for old, new in (
        ("^", "^^"),
        ("&", "^&"),
        ("|", "^|"),
        ("<", "^<"),
        (">", "^>"),
    ):
        text = text.replace(old, new)
    return text


def _usb_config_cmd(config: Dict[str, str]) -> str:
    return "\r\n".join([
        "@echo off",
        f"set \"BRETTER_SERVER_URL={_batch_literal(config.get('server_url', ''))}\"",
        f"set \"BRETTER_IMAGE_SHARE={_batch_literal(config.get('image_share', ''))}\"",
        f"set \"BRETTER_SMB_USERNAME={_batch_literal(config.get('smb_username', ''))}\"",
        f"set \"BRETTER_SMB_PASSWORD={_batch_literal(config.get('smb_password', ''))}\"",
        f"set \"BRETTER_MAP_DRIVE={_batch_literal(config.get('map_drive', 'Z:'))}\"",
        "",
    ])


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _prepare_usb_app_dir(path: str, config: Dict[str, str]) -> None:
    os.makedirs(path, exist_ok=True)
    _write_text(os.path.join(path, "config.json"), json.dumps(config, indent=2))
    _write_text(os.path.join(path, "config.cmd"), _usb_config_cmd(config))
    _write_text(os.path.join(path, "Start-BretterUsb.cmd"), _start_usb_cmd())
    _write_text(os.path.join(path, "ImagingConsole.ps1"), _usb_console_ps1())
    _write_text(os.path.join(path, "ImagingConsole.cmd"), _fallback_usb_cmd())
    driver_target = os.path.join(path, "drivers")
    for full_path, rel_path in _walk_driver_paths():
        target = os.path.join(driver_target, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(full_path, target)
        if full_path.lower().endswith(".cab") and shutil.which("cabextract"):
            expanded_dir = os.path.join(
                driver_target,
                os.path.splitext(rel_path.replace(os.sep, "_"))[0] + "-expanded",
            )
            os.makedirs(expanded_dir, exist_ok=True)
            subprocess.run(
                ["cabextract", "-q", "-d", expanded_dir, full_path],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )


def _startnet_cmd() -> str:
    return r'''@echo off
setlocal EnableDelayedExpansion
set "LOG=X:\BretterIMG\startup.log"
echo Bretter-IMG WinPE startnet > "%LOG%"
echo Bretter-IMG WinPE starting...
echo Startup log: %LOG%
echo.
echo Running wpeinit...
echo Running wpeinit... >> "%LOG%"
wpeinit >> "%LOG%" 2>&1
set "BRETTER_USB_WPEINIT_DONE=1"
echo Loading embedded WinPE driver store...
echo Loading embedded WinPE driver store... >> "%LOG%"
if exist "X:\BretterIMG\drivers" (
  for /r "X:\BretterIMG\drivers" %%I in (*.inf) do (
    echo drvload "%%I" >> "%LOG%"
    drvload "%%I" >> "%LOG%" 2>&1
  )
) else (
  echo No embedded WinPE driver store found at X:\BretterIMG\drivers. >> "%LOG%"
)
echo Rescanning storage and enabling volume automount after driver load...
echo Rescanning storage and enabling volume automount after driver load... >> "%LOG%"
echo rescan | diskpart >> "%LOG%" 2>&1
mountvol /E >nul 2>&1
set "DP=X:\BretterIMG-diskpart.txt"
echo automount enable > "%DP%"
echo rescan >> "%DP%"
for %%N in (0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31) do (
  echo select volume %%N >> "%DP%"
  echo assign noerr >> "%DP%"
)
diskpart /s "%DP%" >> "%LOG%" 2>&1
echo Initializing WinPE networking after driver load...
echo Initializing WinPE networking after driver load... >> "%LOG%"
wpeutil InitializeNetwork >> "%LOG%" 2>&1
netcfg -winpe >> "%LOG%" 2>&1
net start dhcp >> "%LOG%" 2>&1
net start lmhosts >> "%LOG%" 2>&1
net start lanmanworkstation >> "%LOG%" 2>&1
ipconfig /renew >> "%LOG%" 2>&1
ipconfig /all >> "%LOG%" 2>&1
set "BRETTER_USB_DRIVERS_LOADED=1"
set "BRETTER_USB_NETWORK_READY=1"
if exist X:\BretterIMG\Start-BretterUsb.cmd (
  echo Launching Bretter-IMG USB console...
  call X:\BretterIMG\Start-BretterUsb.cmd
) else (
  echo X:\BretterIMG\Start-BretterUsb.cmd was not found.
  echo X:\BretterIMG\Start-BretterUsb.cmd was not found. >> "%LOG%"
)
echo.
echo Bretter-IMG startup finished. Log: %LOG%
cmd /k
'''


def _wim_update_quote(path: str) -> str:
    return '"' + path.replace("\\", "/").replace('"', '\\"') + '"'


def _customize_boot_wim(output_wim: str, config: Dict[str, str], work_dir: str) -> None:
    wimlib_imagex = shutil.which("wimlib-imagex")
    if not wimlib_imagex:
        raise HTTPException(
            status_code=500,
            detail="Cannot build completed ISO: install wimtools so wimlib-imagex is available on the server.",
        )

    shutil.copy2(_asset_path("boot.wim"), output_wim)
    app_dir = os.path.join(work_dir, "BretterIMG")
    _prepare_usb_app_dir(app_dir, config)
    startnet_path = os.path.join(work_dir, "startnet.cmd")
    _write_text(startnet_path, _startnet_cmd())
    update_commands = os.path.join(work_dir, "wim-update.txt")
    _write_text(
        update_commands,
        "\n".join(
            [
                "delete --force --recursive /BretterIMG",
                "delete --force /Windows/System32/winpeshl.ini",
                f"add {_wim_update_quote(app_dir)} /BretterIMG",
                f"add {_wim_update_quote(startnet_path)} /Windows/System32/startnet.cmd",
                "",
            ]
        ),
    )
    env = os.environ.copy()
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    with open(update_commands, "rb") as commands:
        result = subprocess.run(
            [wimlib_imagex, "update", output_wim, "1", "--rebuild"],
            stdin=commands,
            capture_output=True,
            text=True,
            env=env,
            timeout=1800,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "wimlib-imagex update failed").strip()
        raise HTTPException(status_code=500, detail=f"Could not customize boot.wim: {detail[-1200:]}")


def _prepare_windows_iso_tree(iso_root: str, boot_wim: str, work_dir: str) -> tuple[str, str]:
    sources_dir = os.path.join(iso_root, "sources")
    boot_dir = os.path.join(iso_root, "boot")
    efi_boot_dir = os.path.join(iso_root, "efi", "boot")
    efi_ms_boot_dir = os.path.join(iso_root, "efi", "microsoft", "boot")
    for path in (sources_dir, boot_dir, efi_boot_dir, efi_ms_boot_dir):
        os.makedirs(path, exist_ok=True)

    shutil.copy2(boot_wim, os.path.join(sources_dir, "boot.wim"))
    shutil.copy2(_asset_path("boot.sdi"), os.path.join(boot_dir, "boot.sdi"))
    shutil.copy2(_asset_path("boot.sdi"), os.path.join(efi_ms_boot_dir, "boot.sdi"))

    _extract_wim_file("/Windows/Boot/PCAT/bootmgr", os.path.join(iso_root, "bootmgr"), work_dir)
    _extract_wim_file("/Windows/Boot/EFI/bootmgr.efi", os.path.join(iso_root, "bootmgr.efi"), work_dir)
    _extract_wim_file("/Windows/Boot/EFI/bootmgfw.efi", os.path.join(efi_boot_dir, "bootx64.efi"), work_dir)
    _extract_wim_file("/Windows/Boot/DVD/PCAT/BCD", os.path.join(boot_dir, "BCD"), work_dir)
    _extract_wim_file("/Windows/Boot/DVD/EFI/BCD", os.path.join(efi_ms_boot_dir, "BCD"), work_dir)

    etfsboot = _extract_boot_image_asset("etfsboot.com", work_dir, required=True)
    efisys = _extract_boot_image_asset("efisys.bin", work_dir, required=True)
    shutil.copy2(etfsboot, os.path.join(boot_dir, "etfsboot.com"))
    shutil.copy2(efisys, os.path.join(efi_ms_boot_dir, "efisys.bin"))
    return os.path.join(boot_dir, "etfsboot.com"), os.path.join(efi_ms_boot_dir, "efisys.bin")


def _build_completed_iso(iso_path: str, boot_wim: str, work_dir: str) -> None:
    xorriso = shutil.which("xorriso")
    if not xorriso:
        raise HTTPException(
            status_code=500,
            detail="Cannot build completed ISO: install xorriso on the server.",
        )

    iso_root = os.path.join(work_dir, "iso-root")
    os.makedirs(iso_root, exist_ok=True)
    _prepare_windows_iso_tree(iso_root, boot_wim, work_dir)
    result = subprocess.run(
        [
            xorriso,
            "-as",
            "mkisofs",
            "-iso-level",
            "3",
            "-J",
            "-joliet-long",
            "-D",
            "-relaxed-filenames",
            "-V",
            "BRETTER_WINPE",
            "-b",
            "boot/etfsboot.com",
            "-no-emul-boot",
            "-boot-load-size",
            "8",
            "-boot-info-table",
            "-eltorito-alt-boot",
            "-e",
            "efi/microsoft/boot/efisys.bin",
            "-no-emul-boot",
            "-o",
            iso_path,
            iso_root,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "xorriso failed").strip()
        raise HTTPException(status_code=500, detail=f"Could not create bootable ISO: {detail[-1200:]}")


def _build_iso_script() -> str:
    return r'''param(
  [string]$OutputIso = ".\BretterIMG-WinPE.iso"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this PowerShell script as Administrator."
  }
}

function Invoke-Logged {
  param([string]$FilePath, [string[]]$Arguments)
  Write-Host ">" $FilePath ($Arguments -join " ")
  $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -NoNewWindow -Wait -PassThru
  if ($process.ExitCode -ne 0) {
    throw "$FilePath failed with exit code $($process.ExitCode)."
  }
}

function Copy-Tree {
  param([string]$Source, [string]$Destination)
  if (Test-Path $Destination) { Remove-Item $Destination -Recurse -Force }
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
  Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
}

Assert-Admin

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$payload = Join-Path $scriptRoot "payload"
$media = Join-Path $payload "media"
$app = Join-Path $payload "BretterIMG"
$bootWim = Join-Path $media "boot.wim"
$bootSdi = Join-Path $media "boot.sdi"
$isoRoot = Join-Path $env:TEMP ("BretterIsoRoot-" + [Guid]::NewGuid().ToString("N"))

if (-not (Test-Path $bootWim)) { throw "payload\media\boot.wim is missing." }
if (-not (Test-Path $bootSdi)) { throw "payload\media\boot.sdi is missing." }

function Find-Oscdimg {
  $paths = @(
    (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\11\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe")
  )
  foreach ($path in $paths) {
    if ($path -and (Test-Path $path)) { return $path }
  }
  $cmd = Get-Command oscdimg.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "oscdimg.exe was not found. Install the Windows ADK Deployment Tools on this workstation."
}

function Find-AdkBootFile([string]$FileName) {
  $roots = @(
    (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Assessment and Deployment Kit\Deployment Tools"),
    (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\11\Assessment and Deployment Kit\Deployment Tools")
  )
  foreach ($root in $roots) {
    if ($root -and (Test-Path $root)) {
      $match = Get-ChildItem -Path $root -Filter $FileName -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($match) { return $match.FullName }
    }
  }
  throw "$FileName was not found in the Windows ADK Deployment Tools."
}

$mount = Join-Path $env:TEMP ("BretterUsbMount-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $mount -Force | Out-Null
$mounted = $false

try {
  Write-Host "Mounting WinPE image..."
  Invoke-Logged dism.exe @("/Mount-Image", "/ImageFile:$bootWim", "/Index:1", "/MountDir:$mount")
  $mounted = $true

  Write-Host "Injecting Bretter-IMG USB console..."
  $targetApp = Join-Path $mount "BretterIMG"
  Copy-Tree $app $targetApp
  $winpeshl = Join-Path $mount "Windows\System32\winpeshl.ini"
  Set-Content -Path $winpeshl -Encoding ASCII -Value "[LaunchApps]`r`n%SYSTEMROOT%\System32\cmd.exe, /c X:\BretterIMG\Start-BretterUsb.cmd`r`n"

  Write-Host "Uploaded drivers are staged under X:\BretterIMG\drivers and loaded with drvload at WinPE startup."

  Write-Host "Committing WinPE image..."
  Invoke-Logged dism.exe @("/Unmount-Image", "/MountDir:$mount", "/Commit")
  $mounted = $false
} finally {
  if ($mounted) {
    dism.exe /Unmount-Image /MountDir:$mount /Discard | Out-Null
  }
  Remove-Item $mount -Recurse -Force -ErrorAction SilentlyContinue
}

try {
  Write-Host "Building ISO file tree..."
  New-Item -ItemType Directory -Path (Join-Path $isoRoot "sources") -Force | Out-Null
  New-Item -ItemType Directory -Path (Join-Path $isoRoot "boot") -Force | Out-Null
  Copy-Item $bootWim (Join-Path $isoRoot "sources\boot.wim") -Force
  Copy-Item $bootSdi (Join-Path $isoRoot "boot\boot.sdi") -Force

  $oscdimg = Find-Oscdimg
  $etfsboot = Find-AdkBootFile "etfsboot.com"
  $efisys = Find-AdkBootFile "efisys.bin"
  $output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputIso)
  $bootData = "2#p0,e,b$etfsboot#pEF,e,b$efisys"

  Write-Host "Creating $output..."
  Invoke-Logged $oscdimg @("-m", "-o", "-u2", "-udfver102", "-bootdata:$bootData", $isoRoot, $output)
} finally {
  Remove-Item $isoRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Bretter-IMG WinPE ISO is ready: $OutputIso"
Write-Host "Write the ISO to USB with Rufus, Ventoy, iLO/iDRAC virtual media, or another ISO-capable boot tool."
'''


def _start_usb_cmd() -> str:
    return r'''@echo off
title Bretter-IMG USB
set "LOG=X:\BretterIMG\startup.log"
echo Launching Bretter-IMG WinPE console... >> "%LOG%"
echo Ensuring WinPE drivers and networking are initialized before the console opens. >> "%LOG%"
if not "%BRETTER_USB_WPEINIT_DONE%"=="1" call :RUN_WPEINIT
if not "%BRETTER_USB_DRIVERS_LOADED%"=="1" call :LOAD_DRIVER_STORE
if not "%BRETTER_USB_NETWORK_READY%"=="1" call :INIT_NETWORK
cd /d X:\BretterIMG
echo Opening Bretter-IMG imaging menu...
echo Opening Bretter-IMG imaging menu... >> "%LOG%"
call X:\BretterIMG\ImagingConsole.cmd
echo.
echo Bretter-IMG console exited. Startup log: %LOG%
echo Type X:\BretterIMG\ImagingConsole.cmd to open the fallback menu again.
echo Type powershell -NoLogo -NoProfile -Sta -ExecutionPolicy Bypass -File X:\BretterIMG\ImagingConsole.ps1 to try the graphical console.
exit /b 0

:RUN_WPEINIT
echo Running wpeinit...
echo Running wpeinit... >> "%LOG%"
wpeinit >> "%LOG%" 2>&1
set "BRETTER_USB_WPEINIT_DONE=1"
exit /b 0

:LOAD_DRIVER_STORE
echo Loading embedded WinPE driver store with the same method used by capture/deploy...
echo Loading embedded WinPE driver store with the same method used by capture/deploy... >> "%LOG%"
if exist "X:\BretterIMG\drivers" (
  for /r "X:\BretterIMG\drivers" %%I in (*.inf) do (
    echo drvload "%%I" >> "%LOG%"
    drvload "%%I" >> "%LOG%" 2>&1
  )
)
echo rescan | diskpart >> "%LOG%" 2>&1
mountvol /E >nul 2>&1
set "BRETTER_USB_DRIVERS_LOADED=1"
exit /b 0

:INIT_NETWORK
echo Initializing WinPE networking after driver load...
echo Initializing WinPE networking after driver load... >> "%LOG%"
wpeutil InitializeNetwork >> "%LOG%" 2>&1
netcfg -winpe >> "%LOG%" 2>&1
net start dhcp >> "%LOG%" 2>&1
net start lmhosts >> "%LOG%" 2>&1
net start lanmanworkstation >> "%LOG%" 2>&1
ipconfig /renew >> "%LOG%" 2>&1
ipconfig /all >> "%LOG%" 2>&1
set "BRETTER_USB_NETWORK_READY=1"
exit /b 0
'''


def _fallback_usb_cmd() -> str:
    return r'''@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Bretter-IMG Imaging Console
call X:\BretterIMG\config.cmd
set "DRIVE=%BRETTER_MAP_DRIVE%"
set "SHARE=%BRETTER_IMAGE_SHARE%"
set "USB_LOG=X:\BretterIMG\usb-console.log"
set "SMB_USER=%BRETTER_SMB_USERNAME%"
set "SMB_PASS=%BRETTER_SMB_PASSWORD%"
set "SHARE_HOST=%SHARE%"
set "SHARE_HOST=%SHARE_HOST:~2%"
for /f "tokens=1 delims=\" %%S in ("%SHARE_HOST%") do set "SHARE_HOST=%%S"
set "SHARE_LEAF=%SHARE%"
set "SHARE_LEAF=%SHARE_LEAF:~2%"
for /f "tokens=2 delims=\" %%S in ("%SHARE_LEAF%") do set "SHARE_LEAF=%%S"
set "SHARE_NETBIOS="
set "ALT_SHARE="
if "%SMB_USER%"=="" (
  reg add HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f >> "%USB_LOG%" 2>&1
  set "SMB_USER=Guest"
  set "SMB_PASS="
)
call :ENSURE_MAP

:MENU
cls
echo Bretter-IMG Imaging Console
echo.
echo Image share: %SHARE%
echo Mapped drive: %DRIVE%
echo.
echo 1. Create image and save to SMB share
echo 2. Deploy Image
echo 3. Command prompt
echo 4. Reboot
echo.
set "MENU_CHOICE="
set /p MENU_CHOICE=Select 1-4:
if "%MENU_CHOICE%"=="1" goto CAPTURE
if "%MENU_CHOICE%"=="2" goto DEPLOY
if "%MENU_CHOICE%"=="3" goto PROMPT
if "%MENU_CHOICE%"=="4" goto REBOOT
echo Invalid selection.
pause
goto MENU

:REBOOT
set "CONFIRM_REBOOT="
set /p CONFIRM_REBOOT=Type REBOOT to restart:
if /i "%CONFIRM_REBOOT%"=="REBOOT" wpeutil reboot
goto MENU

:PROMPT
cmd
goto MENU

:ENSURE_MAP
if exist "%DRIVE%\" exit /b 0
call :WAIT_NETWORK
set "MAP_ATTEMPT=0"
set "MAP_RC=1"

:MAP_RETRY
set /a MAP_ATTEMPT+=1
echo SMB map attempt !MAP_ATTEMPT! of 18 for %SHARE% as %SMB_USER%...
echo SMB map attempt !MAP_ATTEMPT! of 18 for %SHARE% as %SMB_USER%... >> "%USB_LOG%"
call :TRY_MAP "%SHARE%" "%SMB_USER%"
if not errorlevel 1 exit /b 0
if /i not "%SMB_USER%"=="Guest" (
  echo SMB map failed with error !MAP_RC!; trying server-qualified username.
  echo SMB map failed with error !MAP_RC!; trying server-qualified username. >> "%USB_LOG%"
  call :TRY_MAP "%SHARE%" "%SHARE_HOST%\%SMB_USER%"
  if not errorlevel 1 exit /b 0
)
if "!MAP_ATTEMPT!"=="1" call :SMB_DIAGNOSTICS
if defined ALT_SHARE (
  echo SMB map failed; trying NetBIOS path !ALT_SHARE!.
  echo SMB map failed; trying NetBIOS path !ALT_SHARE!. >> "%USB_LOG%"
  call :TRY_MAP "!ALT_SHARE!" "%SMB_USER%"
  if not errorlevel 1 exit /b 0
  if /i not "%SMB_USER%"=="Guest" (
    call :TRY_MAP "!ALT_SHARE!" "!SHARE_NETBIOS!\%SMB_USER%"
    if not errorlevel 1 exit /b 0
  )
)
if !MAP_ATTEMPT! GEQ 18 goto MAP_FAILED
echo Network or SMB auth is not ready yet. Reinitializing and retrying...
call :WAIT_NETWORK
goto MAP_RETRY

:MAP_FAILED
echo.
echo Could not map %SHARE% to %DRIVE%. NET USE error !MAP_RC!.
echo Could not map %SHARE% to %DRIVE%. NET USE error !MAP_RC!. >> "%USB_LOG%"
echo Check that WinPE has a working NIC driver, IP address, route, DNS, SMB access, and the right username/password.
echo.
ipconfig /all
ipconfig /all >> "%USB_LOG%" 2>&1
echo.
echo SMB log: %USB_LOG%
pause
exit /b 1

:WAIT_NETWORK
echo Preparing WinPE networking...
echo Preparing WinPE networking for %SHARE_HOST%... >> "%USB_LOG%"
wpeutil InitializeNetwork >nul 2>&1
netcfg -winpe >nul 2>&1
net start dhcp >nul 2>&1
net start lmhosts >nul 2>&1
net start lanmanworkstation >nul 2>&1
ipconfig /renew >nul 2>&1
ipconfig /all >> "%USB_LOG%" 2>&1
if not "%SHARE_HOST%"=="" (
  for /L %%R in (1,1,12) do (
    ping -n 1 -w 3000 "%SHARE_HOST%" >nul 2>&1 && exit /b 0
    echo Waiting for SMB host %SHARE_HOST% attempt %%R of 12...
    echo Waiting for SMB host %SHARE_HOST% attempt %%R of 12... >> "%USB_LOG%"
    ping -n 6 127.0.0.1 >nul
  )
)
ping -n 3 127.0.0.1 >nul 2>&1
exit /b 0

:TRY_MAP
net use %DRIVE% /delete /y >nul 2>&1
echo net use %DRIVE% "%~1" /user:"%~2" >> "%USB_LOG%"
net use %DRIVE% "%~1" "%SMB_PASS%" /user:"%~2" /persistent:no >> "%USB_LOG%" 2>&1
set "MAP_RC=%errorlevel%"
exit /b %MAP_RC%

:SMB_DIAGNOSTICS
echo SMB diagnostics for %SHARE_HOST% at %DATE% %TIME% >> "%USB_LOG%"
route print >> "%USB_LOG%" 2>&1
arp -a >> "%USB_LOG%" 2>&1
nbtstat -A "%SHARE_HOST%" >> "%USB_LOG%" 2>&1
for /f "tokens=1" %%N in ('nbtstat -A "%SHARE_HOST%" ^| find "<20>"') do if not defined SHARE_NETBIOS set "SHARE_NETBIOS=%%N"
if defined SHARE_NETBIOS set "ALT_SHARE=\\!SHARE_NETBIOS!\%SHARE_LEAF%"
if defined ALT_SHARE echo NetBIOS SMB fallback path: !ALT_SHARE! >> "%USB_LOG%"
net view "\\%SHARE_HOST%" >> "%USB_LOG%" 2>&1
net use >> "%USB_LOG%" 2>&1
exit /b 0

:DEPLOY
call :ENSURE_MAP
if errorlevel 1 goto MENU
echo.
echo Available WIM images:
echo.
set "WIM_LIST=X:\BretterIMG\wim-list.txt"
dir /s /b "%DRIVE%\*.wim" > "%WIM_LIST%" 2>nul
set "WIM_COUNT=0"
for /f "usebackq delims=" %%F in ("%WIM_LIST%") do (
  set /a WIM_COUNT+=1
  set "WIM_!WIM_COUNT!=%%F"
  echo !WIM_COUNT!. %%F
)
if "%WIM_COUNT%"=="0" (
  echo No WIM files were found on %DRIVE%.
  pause
  goto MENU
)
echo.
set "WIM_SELECTION="
set /p WIM_SELECTION=Select image number:
set "WIM=!WIM_%WIM_SELECTION%!"
if not defined WIM (
  echo Invalid image selection.
  pause
  goto MENU
)
if not exist "%WIM%" (
  echo Selected image was not found: %WIM%
  pause
  goto MENU
)
echo.
echo Available disks:
echo list disk > X:\BretterIMG\diskpart-list.txt
diskpart /s X:\BretterIMG\diskpart-list.txt
echo.
set /p TARGET_DISK=Target disk number:
echo This wipes disk %TARGET_DISK% and applies image index 1.
set /p CONFIRM=Type DEPLOY to continue:
if /i not "%CONFIRM%"=="DEPLOY" goto MENU
(
echo select disk %TARGET_DISK%
echo clean
echo convert gpt
echo create partition efi size=260
echo format quick fs=fat32 label=System
echo assign letter=S
echo create partition msr size=16
echo create partition primary
echo format quick fs=ntfs label=Windows
echo assign letter=W
) > X:\BretterIMG\diskpart-deploy.txt
diskpart /s X:\BretterIMG\diskpart-deploy.txt
dism /Apply-Image /ImageFile:"%WIM%" /Index:1 /ApplyDir:W:\
bcdboot W:\Windows /s S: /f UEFI
pause
goto MENU

:CAPTURE
call :ENSURE_MAP
if errorlevel 1 goto MENU
echo.
echo Available Windows volumes:
echo.
set "VOL_COUNT=0"
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
  if exist %%D:\Windows\System32\config\SYSTEM (
    set /a VOL_COUNT+=1
    set "VOL_!VOL_COUNT!=%%D:"
    echo !VOL_COUNT!. %%D:
  )
)
if "%VOL_COUNT%"=="0" (
  for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if exist %%D:\Windows (
      set /a VOL_COUNT+=1
      set "VOL_!VOL_COUNT!=%%D:"
      echo !VOL_COUNT!. %%D:
    )
  )
)
if "%VOL_COUNT%"=="0" (
  echo No Windows volumes were detected.
  pause
  goto MENU
)
echo.
set "VOL_SELECTION="
set /p VOL_SELECTION=Select Windows volume number:
set "SOURCE_DRIVE=!VOL_%VOL_SELECTION%!"
if not defined SOURCE_DRIVE (
  echo Invalid volume selection.
  pause
  goto MENU
)
set NAME=%COMPUTERNAME%-%DATE:/=-%-%TIME::=-%
set NAME=%NAME: =0%
dism /Capture-Image /ImageFile:"%DRIVE%\%NAME%.wim" /CaptureDir:%SOURCE_DRIVE%\ /Name:"%NAME%" /Compress:max /CheckIntegrity
pause
goto MENU
'''


def _usb_console_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$config = Get-Content "X:\BretterIMG\config.json" -Raw | ConvertFrom-Json
$script:Drive = $config.map_drive
$script:Share = $config.image_share
$script:Username = $config.smb_username
$script:Password = $config.smb_password
$script:ShareHost = (($script:Share -replace '^\\\\', '') -split '\\')[0]
$shareParts = (($script:Share -replace '^\\\\', '') -split '\\')
$script:ShareLeaf = if ($shareParts.Count -gt 1) { $shareParts[1] } else { "" }
$script:NetbiosName = ""
$script:AltShare = ""
if (-not $script:Username) {
  reg.exe add "HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters" /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f | Out-Null
  $script:Username = "Guest"
  $script:Password = ""
}

function Write-Status([string]$message) {
  $status.AppendText("[$(Get-Date -Format HH:mm:ss)] $message`r`n")
  $status.SelectionStart = $status.TextLength
  $status.ScrollToCaret()
  [System.Windows.Forms.Application]::DoEvents()
}

function Invoke-CommandLine([string]$file, [string[]]$arguments) {
  Write-Status ("> " + $file + " " + ($arguments -join " "))
  $process = Start-Process -FilePath $file -ArgumentList $arguments -NoNewWindow -Wait -PassThru
  if ($process.ExitCode -ne 0) {
    throw "$file exited with code $($process.ExitCode)."
  }
}

function Initialize-Network {
  Write-Status "Preparing WinPE networking..."
  try { wpeutil.exe InitializeNetwork | Out-Null } catch { Write-Status $_.Exception.Message }
  try { netcfg.exe -winpe | Out-Null } catch { Write-Status $_.Exception.Message }
  try { net.exe start dhcp | Out-Null } catch { Write-Status $_.Exception.Message }
  try { net.exe start lmhosts | Out-Null } catch { Write-Status $_.Exception.Message }
  try { net.exe start lanmanworkstation | Out-Null } catch { Write-Status $_.Exception.Message }
  try { ipconfig.exe /renew | Out-Null } catch { Write-Status $_.Exception.Message }
  if ($script:ShareHost) {
    for ($wait = 1; $wait -le 12; $wait++) {
      ping.exe -n 1 -w 3000 $script:ShareHost | Out-Null
      if ($LASTEXITCODE -eq 0) { return }
      Write-Status "Waiting for SMB host $script:ShareHost attempt $wait of 12..."
      Start-Sleep -Seconds 5
    }
  }
}

function Invoke-SmbDiagnostics {
  Write-Status "Collecting SMB diagnostics for $script:ShareHost..."
  route.exe print | ForEach-Object { Write-Status $_ }
  arp.exe -a | ForEach-Object { Write-Status $_ }
  $nbt = nbtstat.exe -A $script:ShareHost 2>&1
  $nbt | ForEach-Object { Write-Status $_ }
  foreach ($line in $nbt) {
    if (-not $script:NetbiosName -and $line -match '^\s*([^\s]+)\s+<20>') {
      $script:NetbiosName = $Matches[1]
      if ($script:ShareLeaf) {
        $script:AltShare = "\\$($script:NetbiosName)\$($script:ShareLeaf)"
        Write-Status "NetBIOS SMB fallback path: $script:AltShare"
      }
    }
  }
  net.exe view "\\$($script:ShareHost)" | ForEach-Object { Write-Status $_ }
}

function Try-MapShare([string]$share, [string]$user) {
  net.exe use $script:Drive /delete /y | Out-Null
  Write-Status "net use $script:Drive $share /user:$user"
  net.exe use $script:Drive $share $script:Password /user:$user /persistent:no | Out-Null
  return $LASTEXITCODE
}

function Connect-Share {
  if (Test-Path ($script:Drive + "\")) {
    Write-Status "Share is already mapped at $script:Drive"
    return
  }
  for ($attempt = 1; $attempt -le 18; $attempt++) {
    Initialize-Network
    Write-Status "SMB map attempt $attempt of 18 for $script:Share as $script:Username..."
    $mapRc = Try-MapShare $script:Share $script:Username
    if ($mapRc -eq 0 -and (Test-Path ($script:Drive + "\"))) {
      Write-Status "Mapped $script:Share to $script:Drive"
      return
    }
    if ($script:Username -ne "Guest") {
      Write-Status "SMB map failed with error $mapRc; trying server-qualified username."
      $mapRc = Try-MapShare $script:Share "$($script:ShareHost)\$($script:Username)"
      if ($mapRc -eq 0 -and (Test-Path ($script:Drive + "\"))) {
        Write-Status "Mapped $script:Share to $script:Drive"
        return
      }
    }
    if ($attempt -eq 1) { Invoke-SmbDiagnostics }
    if ($script:AltShare) {
      Write-Status "SMB map failed; trying NetBIOS path $script:AltShare."
      $mapRc = Try-MapShare $script:AltShare $script:Username
      if ($mapRc -eq 0 -and (Test-Path ($script:Drive + "\"))) {
        Write-Status "Mapped $script:AltShare to $script:Drive"
        return
      }
      if ($script:Username -ne "Guest" -and $script:NetbiosName) {
        $mapRc = Try-MapShare $script:AltShare "$($script:NetbiosName)\$($script:Username)"
        if ($mapRc -eq 0 -and (Test-Path ($script:Drive + "\"))) {
          Write-Status "Mapped $script:AltShare to $script:Drive"
          return
        }
      }
    }
    Start-Sleep -Seconds 10
  }
  ipconfig.exe /all | ForEach-Object { Write-Status $_ }
  throw "Could not map $script:Share to $script:Drive. NET USE error $mapRc. Check NIC driver, IP address, route, DNS, SMB access, and the right username/password."
}

function Refresh-Images {
  $images.Items.Clear()
  if (-not (Test-Path ($script:Drive + "\"))) {
    Connect-Share
  }
  Get-ChildItem -Path ($script:Drive + "\") -Filter *.wim -File -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName |
    ForEach-Object { [void]$images.Items.Add($_.FullName) }
  Write-Status ("Found {0} WIM image(s)." -f $images.Items.Count)
}

function Get-WindowsVolumes {
  Get-PSDrive -PSProvider FileSystem |
    Where-Object { Test-Path (Join-Path $_.Root "Windows") } |
    ForEach-Object { $_.Root.TrimEnd("\") }
}

function Get-DiskChoices {
  Get-CimInstance Win32_DiskDrive |
    Sort-Object Index |
    ForEach-Object {
      $gb = if ($_.Size) { [math]::Round($_.Size / 1GB, 1) } else { 0 }
      "Disk $($_.Index) - $($_.Model) - $gb GB"
    }
}

function Deploy-Selected {
  if (-not $images.SelectedItem) {
    [System.Windows.Forms.MessageBox]::Show("Select an image first.", "Bretter-IMG")
    return
  }
  if (-not $targetDisk.SelectedItem -or $targetDisk.SelectedItem -notmatch "^Disk\s+(\d+)") {
    [System.Windows.Forms.MessageBox]::Show("Select a target disk first.", "Bretter-IMG")
    return
  }
  $diskNumber = $Matches[1]
  $confirm = [System.Windows.Forms.MessageBox]::Show(
    "This will wipe disk $diskNumber, create UEFI partitions, apply image index 1, and reboot when complete.",
    "Deploy image",
    [System.Windows.Forms.MessageBoxButtons]::OKCancel,
    [System.Windows.Forms.MessageBoxIcon]::Warning
  )
  if ($confirm -ne [System.Windows.Forms.DialogResult]::OK) { return }

  $script = "X:\BretterIMG\diskpart-deploy.txt"
  @(
    "select disk $diskNumber",
    "clean",
    "convert gpt",
    "create partition efi size=260",
    "format quick fs=fat32 label=System",
    "assign letter=S",
    "create partition msr size=16",
    "create partition primary",
    "format quick fs=ntfs label=Windows",
    "assign letter=W"
  ) | Set-Content -Path $script -Encoding ASCII

  Write-Status "Preparing disk $diskNumber..."
  Invoke-CommandLine "diskpart.exe" @("/s", $script)
  Write-Status "Applying image..."
  Invoke-CommandLine "dism.exe" @("/Apply-Image", "/ImageFile:$($images.SelectedItem)", "/Index:1", "/ApplyDir:W:\")
  Write-Status "Creating UEFI boot files..."
  Invoke-CommandLine "bcdboot.exe" @("W:\Windows", "/s", "S:", "/f", "UEFI")
  Write-Status "Deployment complete."
}

function Capture-Image {
  if (-not (Test-Path ($script:Drive + "\"))) {
    Connect-Share
  }
  $source = $captureSource.Text.Trim()
  if (-not $source.EndsWith("\")) { $source += "\" }
  if (-not (Test-Path $source)) {
    [System.Windows.Forms.MessageBox]::Show("Capture source was not found.", "Bretter-IMG")
    return
  }
  $safeName = ($captureName.Text.Trim() -replace '[<>:"/\\|?*]', "_")
  if (-not $safeName) { $safeName = "$env:COMPUTERNAME-$(Get-Date -Format yyyyMMdd-HHmm)" }
  $imagePath = Join-Path ($script:Drive + "\") ($safeName + ".wim")
  Write-Status "Capturing $source to $imagePath..."
  Invoke-CommandLine "dism.exe" @("/Capture-Image", "/ImageFile:$imagePath", "/CaptureDir:$source", "/Name:$safeName", "/Compress:max", "/CheckIntegrity")
  Write-Status "Capture complete."
}

$form = [System.Windows.Forms.Form]::new()
$form.Text = "Bretter-IMG WinPE USB"
$form.Size = [System.Drawing.Size]::new(920, 620)
$form.StartPosition = "CenterScreen"
$form.BackColor = [System.Drawing.Color]::FromArgb(17, 24, 39)
$form.ForeColor = [System.Drawing.Color]::White

$title = [System.Windows.Forms.Label]::new()
$title.Text = "Bretter-IMG Imaging Console"
$title.Font = [System.Drawing.Font]::new("Segoe UI", 18, [System.Drawing.FontStyle]::Bold)
$title.Location = [System.Drawing.Point]::new(22, 18)
$title.Size = [System.Drawing.Size]::new(560, 36)
$form.Controls.Add($title)

$shareLabel = [System.Windows.Forms.Label]::new()
$shareLabel.Text = "Image share: $script:Share"
$shareLabel.Location = [System.Drawing.Point]::new(24, 60)
$shareLabel.Size = [System.Drawing.Size]::new(780, 24)
$form.Controls.Add($shareLabel)

$images = [System.Windows.Forms.ListBox]::new()
$images.Location = [System.Drawing.Point]::new(24, 100)
$images.Size = [System.Drawing.Size]::new(540, 245)
$images.BackColor = [System.Drawing.Color]::FromArgb(31, 41, 55)
$images.ForeColor = [System.Drawing.Color]::White
$form.Controls.Add($images)

$refresh = [System.Windows.Forms.Button]::new()
$refresh.Text = "Refresh Images"
$refresh.Location = [System.Drawing.Point]::new(586, 100)
$refresh.Size = [System.Drawing.Size]::new(280, 40)
$refresh.Add_Click({ try { Refresh-Images } catch { Write-Status $_.Exception.Message } })
$form.Controls.Add($refresh)

$targetLabel = [System.Windows.Forms.Label]::new()
$targetLabel.Text = "Deploy target disk"
$targetLabel.Location = [System.Drawing.Point]::new(586, 154)
$targetLabel.Size = [System.Drawing.Size]::new(180, 20)
$form.Controls.Add($targetLabel)

$targetDisk = [System.Windows.Forms.ComboBox]::new()
$targetDisk.Location = [System.Drawing.Point]::new(586, 178)
$targetDisk.Size = [System.Drawing.Size]::new(280, 28)
Get-DiskChoices | ForEach-Object { [void]$targetDisk.Items.Add($_) }
if ($targetDisk.Items.Count -gt 0) { $targetDisk.SelectedIndex = 0 }
$form.Controls.Add($targetDisk)

$deploy = [System.Windows.Forms.Button]::new()
$deploy.Text = "Deploy Image"
$deploy.Location = [System.Drawing.Point]::new(586, 220)
$deploy.Size = [System.Drawing.Size]::new(280, 44)
$deploy.Add_Click({ try { Deploy-Selected } catch { Write-Status $_.Exception.Message } })
$form.Controls.Add($deploy)

$sourceLabel = [System.Windows.Forms.Label]::new()
$sourceLabel.Text = "Capture source"
$sourceLabel.Location = [System.Drawing.Point]::new(586, 292)
$sourceLabel.Size = [System.Drawing.Size]::new(160, 20)
$form.Controls.Add($sourceLabel)

$captureSource = [System.Windows.Forms.ComboBox]::new()
$captureSource.Location = [System.Drawing.Point]::new(586, 316)
$captureSource.Size = [System.Drawing.Size]::new(280, 28)
Get-WindowsVolumes | ForEach-Object { [void]$captureSource.Items.Add($_ + "\") }
if ($captureSource.Items.Count -gt 0) { $captureSource.SelectedIndex = 0 } else { $captureSource.Text = "C:\" }
$form.Controls.Add($captureSource)

$nameLabel = [System.Windows.Forms.Label]::new()
$nameLabel.Text = "Image name"
$nameLabel.Location = [System.Drawing.Point]::new(586, 358)
$nameLabel.Size = [System.Drawing.Size]::new(160, 20)
$form.Controls.Add($nameLabel)

$captureName = [System.Windows.Forms.TextBox]::new()
$captureName.Location = [System.Drawing.Point]::new(586, 382)
$captureName.Size = [System.Drawing.Size]::new(280, 28)
$captureName.Text = "$env:COMPUTERNAME-$(Get-Date -Format yyyyMMdd-HHmm)"
$form.Controls.Add($captureName)

$capture = [System.Windows.Forms.Button]::new()
$capture.Text = "Capture To Share"
$capture.Location = [System.Drawing.Point]::new(586, 424)
$capture.Size = [System.Drawing.Size]::new(280, 44)
$capture.Add_Click({ try { Capture-Image } catch { Write-Status $_.Exception.Message } })
$form.Controls.Add($capture)

$cmd = [System.Windows.Forms.Button]::new()
$cmd.Text = "Command Prompt"
$cmd.Location = [System.Drawing.Point]::new(586, 490)
$cmd.Size = [System.Drawing.Size]::new(136, 36)
$cmd.Add_Click({ Start-Process cmd.exe })
$form.Controls.Add($cmd)

$reboot = [System.Windows.Forms.Button]::new()
$reboot.Text = "Reboot"
$reboot.Location = [System.Drawing.Point]::new(730, 490)
$reboot.Size = [System.Drawing.Size]::new(136, 36)
$reboot.Add_Click({ wpeutil reboot })
$form.Controls.Add($reboot)

$status = [System.Windows.Forms.TextBox]::new()
$status.Location = [System.Drawing.Point]::new(24, 365)
$status.Size = [System.Drawing.Size]::new(540, 185)
$status.Multiline = $true
$status.ScrollBars = "Vertical"
$status.ReadOnly = $true
$status.BackColor = [System.Drawing.Color]::FromArgb(3, 7, 18)
$status.ForeColor = [System.Drawing.Color]::FromArgb(209, 213, 219)
$form.Controls.Add($status)

$form.Add_Shown({ try { Connect-Share; Refresh-Images } catch { Write-Status $_.Exception.Message } })
[void]$form.ShowDialog()
'''


def _bootable_usb_readme() -> str:
    return """Bretter-IMG WinPE ISO Package

1. Extract this ZIP on a Windows admin workstation.
2. Install the Windows ADK Deployment Tools if oscdimg.exe is not already available.
3. Open PowerShell as Administrator.
4. Run:

   .\\Make-BootableIso.ps1 -OutputIso .\\BretterIMG-WinPE.iso

The builder injects the Bretter-IMG imaging console into boot.wim and stages
uploaded WinPE drivers for WinPE startup loading with drvload. It then uses
Microsoft oscdimg.exe and ADK boot sectors to create BretterIMG-WinPE.iso. Write
the ISO to USB with Rufus, Ventoy, iLO/iDRAC virtual media, or another
ISO-capable boot tool.

Inside WinPE, the console maps the configured SMB image share and lets the user
select a target disk for deployment, or capture a selected Windows volume
directly to the SMB share root.
"""


def _extract_driver_zip(zip_path: str) -> int:
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            filename = os.path.basename(member.filename)
            if not filename or not _is_driver_file(filename):
                continue
            target = _driver_path(filename)
            with archive.open(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            extracted += 1
    return extracted


@router.get("/assets/status")
def get_winpe_asset_status(_=Depends(get_current_user)):
    assets = [_asset_info(filename) for filename in sorted(WINPE_ASSET_FILES)]
    required_assets = [asset for asset in assets if asset["required"]]
    return {
        "ready": all(asset["available"] for asset in required_assets),
        "assets": assets,
        "drivers": _driver_files(),
        "config": _read_config(),
    }


@router.get("/config")
def get_winpe_config(_=Depends(get_current_user)):
    return _read_config()


@router.patch("/config")
def update_winpe_config(body: WinPEConfigUpdate, _=Depends(get_current_user)):
    boot_description = body.boot_description.strip()
    if not boot_description:
        raise HTTPException(status_code=400, detail="Boot description is required")
    if len(boot_description) > 80:
        raise HTTPException(status_code=400, detail="Boot description must be 80 characters or less")
    drive = body.direct_capture_drive.strip().upper() or "Z:"
    if len(drive) == 1:
        drive = drive + ":"
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise HTTPException(status_code=400, detail="Direct capture drive must look like Z:")
    share = body.direct_capture_share.strip()
    if body.direct_capture_enabled and not share.startswith("\\\\"):
        raise HTTPException(status_code=400, detail="Direct capture share must be a UNC path like \\\\server\\share")
    return _write_config({
        "boot_description": boot_description,
        "direct_capture_enabled": "true" if body.direct_capture_enabled else "false",
        "direct_capture_share": share,
        "direct_capture_username": body.direct_capture_username.strip(),
        "direct_capture_password": body.direct_capture_password,
        "direct_capture_drive": drive,
    })


@router.get("/config/agent")
def get_agent_winpe_config(_agent_id: str = Depends(get_current_agent_machine_id)):
    return _read_config()


@router.post("/assets/{filename}")
def upload_winpe_asset(
    filename: str,
    file: UploadFile = File(...),
    _=Depends(get_current_user),
):
    path = _asset_path(filename)
    os.makedirs(settings.WINPE_STORE_PATH, exist_ok=True)

    temp_path = f"{path}.uploading"
    with open(temp_path, "wb") as dest:
        shutil.copyfileobj(file.file, dest)
    os.replace(temp_path, path)

    return _asset_info(filename)


@router.get("/drivers/status")
def get_winpe_drivers(_=Depends(get_current_user)):
    return {"drivers": _driver_files()}


@router.get("/drivers/agent")
def get_agent_winpe_drivers(_agent_id: str = Depends(get_current_agent_machine_id)):
    return {"drivers": _driver_files()}


@router.post("/drivers")
def upload_winpe_driver(
    file: UploadFile = File(...),
    _=Depends(get_current_user),
):
    os.makedirs(_drivers_dir(), exist_ok=True)
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="Driver filename is required")

    lower = filename.lower()
    if lower.endswith(".zip"):
        temp_path = os.path.join(_drivers_dir(), "_upload.zip")
        with open(temp_path, "wb") as dest:
            shutil.copyfileobj(file.file, dest)
        try:
            extracted = _extract_driver_zip(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        if not extracted:
            raise HTTPException(status_code=400, detail="Zip did not contain supported driver files")
        return {"ok": True, "extracted": extracted, "drivers": _driver_files()}

    if not _is_driver_file(filename):
        raise HTTPException(status_code=400, detail="Upload .inf/.sys/.cat driver files, a .cab, or a .zip package")

    path = _driver_path(filename)
    with open(path, "wb") as dest:
        shutil.copyfileobj(file.file, dest)
    return {"ok": True, "driver": _file_info(path, filename), "drivers": _driver_files()}


@router.delete("/drivers/{filename}")
def delete_winpe_driver(filename: str, _=Depends(get_current_user)):
    path = _driver_path(filename)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True, "filename": filename}


@router.get("/bootable-usb/status")
def get_bootable_usb_status(_=Depends(get_current_user)):
    iso_asset_names = REQUIRED_BOOTABLE_ISO_ASSET_FILES | OPTIONAL_BOOTABLE_ISO_ASSET_FILES
    assets = [_asset_info(filename) for filename in sorted(iso_asset_names)]
    required_ready = all(_bootable_iso_asset_ready(filename) for filename in REQUIRED_BOOTABLE_ISO_ASSET_FILES)
    drivers = _driver_files()
    config = _read_config()
    share_ready = bool(config.get("direct_capture_share", "").strip().startswith("\\\\"))
    tools = _iso_tool_status()
    tools_ready = bool(tools["wimlib_imagex"] and tools["xorriso"])
    return {
        "ready": required_ready and share_ready and tools_ready,
        "assets_ready": required_ready,
        "tools_ready": tools_ready,
        "tools": tools,
        "share_ready": share_ready,
        "driver_count": len(drivers),
        "drivers": drivers,
        "assets": assets,
        "config": config,
    }


@router.post("/bootable-usb/package")
def generate_bootable_usb_package(
    body: BootableUsbPackageRequest,
    background_tasks: BackgroundTasks,
    _=Depends(get_current_user),
):
    missing = [
        filename
        for filename in REQUIRED_BOOTABLE_ISO_ASSET_FILES
        if not _bootable_iso_asset_ready(filename)
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing WinPE asset(s): {', '.join(sorted(missing))}")
    config = _bootable_usb_config(body)
    os.makedirs(settings.WINPE_STORE_PATH, exist_ok=True)
    fd, iso_path = tempfile.mkstemp(prefix="BretterIMG-WinPE-", suffix=".iso", dir=settings.WINPE_STORE_PATH)
    os.close(fd)
    os.remove(iso_path)

    try:
        with tempfile.TemporaryDirectory(prefix="BretterIMG-WinPE-build-", dir=settings.WINPE_STORE_PATH) as work_dir:
            custom_wim = os.path.join(work_dir, "boot.wim")
            _customize_boot_wim(custom_wim, config, work_dir)
            _build_completed_iso(iso_path, custom_wim, work_dir)
    except Exception:
        if os.path.exists(iso_path):
            os.remove(iso_path)
        raise

    background_tasks.add_task(lambda path: os.path.exists(path) and os.remove(path), iso_path)
    return FileResponse(iso_path, media_type="application/x-iso9660-image", filename="BretterIMG-WinPE.iso")


@router.get("/drivers/{filename}")
def download_winpe_driver(
    filename: str,
    _agent_id: str = Depends(get_current_agent_machine_id),
):
    path = _driver_path(filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Driver file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@router.delete("/assets/{filename}")
def delete_winpe_asset(filename: str, _=Depends(get_current_user)):
    path = _asset_path(filename)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True, "filename": filename}


@router.get("/assets/{filename}")
def download_winpe_asset(
    filename: str,
    _agent_id: str = Depends(get_current_agent_machine_id),
):
    if filename == "background.jpg":
        return Response(
            content=b"",
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="background.jpg"'},
        )
    path = _asset_path(filename)
    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail=f"{filename} has not been uploaded to the server",
        )
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
