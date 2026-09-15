"""
System information collector for Windows.
Falls back gracefully on non-Windows (for dev/testing).
"""

import platform
import socket
import uuid
import json

try:
    import wmi  # type: ignore  (Windows only)
    _HAS_WMI = True
except ImportError:
    _HAS_WMI = False

from config import AGENT_VERSION


def _mac_address() -> str:
    mac = uuid.getnode()
    return ":".join(f"{(mac >> (i * 8)) & 0xff:02x}" for i in range(5, -1, -1))


def collect_sysinfo() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "ip_address": socket.gethostbyname(socket.gethostname()),
        "mac_address": _mac_address(),
        "os_version": platform.version(),
        "agent_version": AGENT_VERSION,
        "cpu": platform.processor(),
        "ram_gb": None,
        "disk_info": None,
    }

    if _HAS_WMI:
        try:
            c = wmi.WMI()
            # RAM
            mem = c.Win32_ComputerSystem()[0]
            gb = round(int(mem.TotalPhysicalMemory) / (1024 ** 3), 1)
            info["ram_gb"] = str(gb)

            # Disks
            disks = []
            for disk in c.Win32_DiskDrive():
                disks.append({
                    "model": disk.Model,
                    "size_gb": round(int(disk.Size or 0) / (1024 ** 3), 1),
                    "interface": disk.InterfaceType,
                })
            info["disk_info"] = json.dumps(disks)

            # OS caption
            os_info = c.Win32_OperatingSystem()[0]
            info["os_version"] = f"{os_info.Caption} ({os_info.Version})"
        except Exception:
            pass

    return info
