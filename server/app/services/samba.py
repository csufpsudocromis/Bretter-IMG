import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from typing import Optional

from ..core.config import settings
from ..models.user import User


USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


class SambaProvisionError(RuntimeError):
    pass


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_RE.match(normalized):
        raise ValueError(
            "Username must start with a letter and contain only lowercase letters, numbers, hyphens, or underscores"
        )
    return normalized


def generate_smb_password() -> str:
    return secrets.token_urlsafe(18)


def provision_samba_access(user: User, password: Optional[str] = None) -> None:
    if not settings.SAMBA_AUTO_PROVISION_USERS:
        user.smb_access_enabled = False
        return

    smbpasswd = shutil.which("smbpasswd")
    groupadd = shutil.which("groupadd")
    useradd = shutil.which("useradd")
    usermod = shutil.which("usermod")
    getent = shutil.which("getent")
    if not all([smbpasswd, groupadd, useradd, usermod, getent]):
        raise SambaProvisionError("Samba/user management tools are not installed on this server")

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise SambaProvisionError("Run the server as root or configure passwordless sudo for Samba provisioning")
        prefix = [sudo, "-n"]
    else:
        prefix = []

    smb_username = user.smb_username or normalize_username(user.username)
    smb_password = password or user.smb_password or generate_smb_password()

    def run(args: list[str], input_text: Optional[str] = None) -> None:
        result = subprocess.run(
            prefix + args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SambaProvisionError(detail or f"Command failed: {' '.join(args)}")

    run([groupadd, "-f", settings.SAMBA_GROUP])

    exists = subprocess.run(
        prefix + [getent, "passwd", smb_username],
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode == 0
    if not exists:
        run([
            useradd,
            "--system",
            "--no-create-home",
            "--shell",
            settings.SAMBA_LOGIN_SHELL,
            "--gid",
            settings.SAMBA_GROUP,
            smb_username,
        ])
    else:
        run([usermod, "-a", "-G", settings.SAMBA_GROUP, smb_username])

    run([smbpasswd, "-s", "-a", smb_username], input_text=f"{smb_password}\n{smb_password}\n")
    run([smbpasswd, "-e", smb_username])

    user.smb_username = smb_username
    user.smb_password = smb_password
    user.smb_access_enabled = True
    user.smb_provisioned_at = datetime.utcnow()
