"""
Installer generation service.

On-the-fly builds a fully self-contained Windows installer ZIP containing:
  - All agent Python source files
  - config.py with server URL + agent token baked in
  - install.bat configured for the bundled Python
  - python/ — bundled Python 3.12 embeddable + pre-installed packages
    (requests, wmi, pywin32, certifi, urllib3, etc.)
  - README.txt with quick-start instructions

No Python or any other software needs to be pre-installed on the target machine.
"""

import io
import os
import zipfile
from datetime import datetime

TEMPLATE_DIR     = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "agent_template"))
AGENT_SOURCE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "agent"))
BUNDLE_DIR       = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "python_bundle"))
BUNDLE_ZIP       = os.path.join(BUNDLE_DIR, "python-bundle.zip")
SERVER_CERT      = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".certs", "bretter-img.crt"))

AGENT_VERSION = "1.0.13"
AGENT_FILES   = ["agent.py", "imaging.py", "sysinfo.py", "transfer.py", "agent_build.txt"]
NSSM_EXE      = os.path.join(TEMPLATE_DIR, "nssm.exe")

# Path where the bundled Python ends up on the target machine
BUNDLED_PYTHON_EXE = r"C:\ProgramData\BretterIMG\python\python.exe"


def _render(template_path: str, replacements: dict) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def _read_agent_file(filename: str) -> bytes:
    template_path = os.path.join(TEMPLATE_DIR, filename)
    if os.path.exists(template_path):
        with open(template_path, "rb") as f:
            return f.read()
    source_path = os.path.join(AGENT_SOURCE_DIR, filename)
    with open(source_path, "rb") as f:
        return f.read()


def bundle_available() -> bool:
    return os.path.exists(BUNDLE_ZIP)


def build_installer_zip(server_url: str, agent_token: str, machine_id: str = "") -> bytes:
    """
    Build and return an in-memory ZIP file ready to serve as a download.
    If the Python bundle is available it is embedded; otherwise a warning
    note is added telling users to install Python manually.
    """
    has_bundle = bundle_available()

    replacements = {
        "{{SERVER_URL}}":      server_url.rstrip("/"),
        "{{AGENT_TOKEN}}":     agent_token,
        "{{AGENT_VERSION}}":   AGENT_VERSION,
        "{{PYTHON_EXE_PATH}}": BUNDLED_PYTHON_EXE if has_bundle else "python",
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:

        # ── Rendered config.py ────────────────────────────────────────────────
        config_content = _render(
            os.path.join(TEMPLATE_DIR, "config.py.template"), replacements
        )
        zf.writestr("BretterIMG-Agent/config.py", config_content)

        # ── Rendered install.bat ──────────────────────────────────────────────
        install_content = _render(
            os.path.join(TEMPLATE_DIR, "install.bat.template"), replacements
        )
        zf.writestr("BretterIMG-Agent/install.bat", install_content)

        # ── Agent source files ────────────────────────────────────────────────
        for fname in AGENT_FILES:
            zf.writestr(f"BretterIMG-Agent/{fname}", _read_agent_file(fname))

        if os.path.exists(SERVER_CERT):
            with open(SERVER_CERT, "rb") as f:
                zf.writestr("BretterIMG-Agent/bretter-img-ca.crt", f.read())

        # ── Pre-seed the machine ID file so agent uses the same ID the token was issued for ──
        if machine_id:
            zf.writestr("BretterIMG-Agent/machine_id.txt", machine_id)

        # ── NSSM service manager ──────────────────────────────────────────────
        if os.path.exists(NSSM_EXE):
            with open(NSSM_EXE, "rb") as f:
                zf.writestr("BretterIMG-Agent/nssm.exe", f.read())

        # ── Bundled Python (streamed from cached bundle ZIP) ──────────────────
        if has_bundle:
            with zipfile.ZipFile(BUNDLE_ZIP, "r") as bundle:
                for item in bundle.infolist():
                    # Remap: python/python.exe → BretterIMG-Agent/python/python.exe
                    arcname = f"BretterIMG-Agent/{item.filename}"
                    zf.writestr(arcname, bundle.read(item.filename))

        # ── README ────────────────────────────────────────────────────────────
        python_note = (
            "✅ Python 3.12 + all dependencies are INCLUDED — no installation needed."
            if has_bundle else
            "⚠️  Python bundle not found. Install Python 3.10+ on the target machine first."
        )
        readme = f"""Bretter-IMG Agent Installer
===========================
Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
Server    : {server_url}
Version   : {AGENT_VERSION}
Python    : {python_note}

QUICK START (no prerequisites needed)
--------------------------------------
1. Extract this ZIP on the target Windows machine
2. Right-click install.bat → "Run as administrator"
3. The agent installs itself as a Windows service and connects to:
   {server_url}
4. The machine appears in the Bretter-IMG console within 30 seconds.

WHAT IS INCLUDED
----------------
  python/          — Python {AGENT_VERSION.split('.')[0]} embeddable (no system install required)
  python/Lib/site-packages/
                   — requests, wmi, pywin32, certifi, urllib3, idna
  agent.py         — Main agent loop
  imaging.py       — DISM capture / deploy / wipe
  sysinfo.py       — Hardware inventory (WMI)
  transfer.py      — HTTP client
  config.py        — Pre-configured for {server_url}

SERVICE MANAGEMENT (after install)
------------------------------------
  net start  BretterIMGAgent   (start)
  net stop   BretterIMGAgent   (stop)
  sc delete  BretterIMGAgent   (uninstall)

LOG FILE
--------
  C:\\ProgramData\\BretterIMG\\logs\\agent.log

CONSOLE / API DOCS
------------------
  {server_url.rstrip('/').replace('/api', '')}/docs
"""
        zf.writestr("BretterIMG-Agent/README.txt", readme)

    return buf.getvalue()
