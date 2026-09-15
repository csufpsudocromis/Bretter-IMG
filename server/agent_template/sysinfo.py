"""
System information collector for Windows.
Falls back gracefully on non-Windows (for dev/testing).
"""

import platform
import socket
import uuid
import json
import os

try:
    import wmi  # type: ignore  (Windows only)
    _HAS_WMI = True
except ImportError:
    _HAS_WMI = False

AGENT_VERSION = "1.0.14"

AGENT_BUILD = "2026-09-11-detailed-cpu"


def _agent_build() -> str:
    build_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_build.txt")
    try:
        with open(build_path, "r", encoding="utf-8") as f:
            build = f.read().strip()
            if build:
                return build
    except OSError:
        pass
    return AGENT_BUILD


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
        "cpu": platform.processor(),  # fallback; overridden by WMI below
        "ram_gb": None,
        "disk_info": None,
    }

    if _HAS_WMI:
        try:
            c = wmi.WMI()

            # CPU — use the friendly name from Win32_Processor (e.g. "Intel(R) Core(TM) i7-14700K CPU @ 3.40GHz")
            try:
                proc = c.Win32_Processor()[0]
                cpu_name = (proc.Name or "").strip()
                if cpu_name:
                    info["cpu"] = cpu_name
            except Exception:
                pass

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
