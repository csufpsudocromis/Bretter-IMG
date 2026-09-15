"""
build_python_bundle.py — Run ONCE on the server to prepare the Python bundle.

Downloads Python 3.12 Windows embeddable package, patches it so pip works,
installs all agent dependencies (requests, wmi, pywin32), then saves the
result as a ready-to-ship ZIP at server/python_bundle/python-bundle.zip

Usage:
    python3 scripts/build_python_bundle.py

Requires: Python 3.x, pip, internet access
Re-run to upgrade the bundle (e.g. after updating PACKAGES below).
"""

import os
import sys
import zipfile
import urllib.request
import subprocess
import shutil
import tempfile

# ── Config ────────────────────────────────────────────────────────────────────
PYTHON_VERSION  = "3.12.10"
PYTHON_ZIP_URL  = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL     = "https://bootstrap.pypa.io/get-pip.py"

# Packages to pre-install into the bundle
# wmi + pywin32 are Windows-only but we download them anyway for the bundle
PACKAGES = [
    "requests",
    "wmi",
    "pywin32",
]

OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "server", "python_bundle")
OUTPUT_ZIP  = os.path.join(OUTPUT_DIR, "python-bundle.zip")
# ──────────────────────────────────────────────────────────────────────────────


def log(msg: str):
    print(f"  {msg}", flush=True)


def download(url: str, dest: str):
    log(f"Downloading {url.split('/')[-1]} …")
    urllib.request.urlretrieve(url, dest)
    log(f"  → {dest} ({os.path.getsize(dest) // 1024:,} KB)")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with tempfile.TemporaryDirectory() as work:
        python_zip = os.path.join(work, "python-embed.zip")
        get_pip    = os.path.join(work, "get-pip.py")
        embed_dir  = os.path.join(work, "python")

        # ── Step 1: Download Python embeddable ────────────────────────────────
        print("\n[1/5] Downloading Python embeddable package …")
        download(PYTHON_ZIP_URL, python_zip)

        # ── Step 2: Extract ───────────────────────────────────────────────────
        print("\n[2/5] Extracting Python …")
        os.makedirs(embed_dir, exist_ok=True)
        with zipfile.ZipFile(python_zip) as zf:
            zf.extractall(embed_dir)
        log(f"Extracted to {embed_dir}")

        # ── Step 3: Enable pip in embeddable Python ───────────────────────────
        # The ._pth file must have 'import site' uncommented so pip packages work
        print("\n[3/5] Patching embeddable Python to support pip …")
        pth_files = [f for f in os.listdir(embed_dir) if f.endswith("._pth")]
        for pth in pth_files:
            pth_path = os.path.join(embed_dir, pth)
            with open(pth_path, "r") as f:
                content = f.read()
            # Uncomment #import site
            patched = content.replace("#import site", "import site")
            with open(pth_path, "w") as f:
                f.write(patched)
            log(f"Patched {pth}")

        # ── Step 4: Install pip then packages ─────────────────────────────────
        print("\n[4/5] Installing pip and packages …")
        download(GET_PIP_URL, get_pip)

        python_exe = os.path.join(embed_dir, "python.exe")
        site_packages = os.path.join(embed_dir, "Lib", "site-packages")

        # We can't actually run python.exe on Linux — instead use the current
        # Python to download wheels for Windows x64 and place them in the bundle
        log("Downloading Windows wheels (cross-platform) …")
        wheels_dir = os.path.join(work, "wheels")
        os.makedirs(wheels_dir, exist_ok=True)

        subprocess.run([
            sys.executable, "-m", "pip", "download",
            "--dest", wheels_dir,
            "--platform", "win_amd64",
            "--python-version", "3.12",
            "--only-binary=:all:",
            "--no-deps",
        ] + PACKAGES, check=False)  # some packages may not have win_amd64 wheels; that's OK

        # Also grab pure-python deps of requests (charset-normalizer, idna, certifi, urllib3)
        subprocess.run([
            sys.executable, "-m", "pip", "download",
            "--dest", wheels_dir,
            "--platform", "win_amd64",
            "--python-version", "3.12",
            "--only-binary=:all:",
            "requests", "certifi", "charset-normalizer", "idna", "urllib3",
        ], check=True)

        log(f"Downloaded {len(os.listdir(wheels_dir))} wheel(s)")

        # Extract all wheels into the embed python's Lib/site-packages
        site_pkg = os.path.join(embed_dir, "Lib", "site-packages")
        os.makedirs(site_pkg, exist_ok=True)
        for wheel in os.listdir(wheels_dir):
            if wheel.endswith(".whl"):
                log(f"Installing {wheel} …")
                with zipfile.ZipFile(os.path.join(wheels_dir, wheel)) as whl:
                    whl.extractall(site_pkg)

        # Add Lib/site-packages to the ._pth so Python finds them
        for pth in pth_files:
            pth_path = os.path.join(embed_dir, pth)
            with open(pth_path, "a") as f:
                f.write("\nLib\\site-packages\n")
            log(f"Added site-packages path to {pth}")

        # ── Step 5: Repack as ZIP ──────────────────────────────────────────────
        print(f"\n[5/5] Building bundle ZIP → {OUTPUT_ZIP} …")
        with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
            for root, dirs, files in os.walk(embed_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, work)  # e.g. python/python.exe
                    zout.write(full_path, arcname)

        size_mb = os.path.getsize(OUTPUT_ZIP) / (1024 * 1024)
        print(f"\n✅ Bundle ready: {OUTPUT_ZIP} ({size_mb:.1f} MB)")
        print("   Re-run this script to refresh the bundle.\n")


if __name__ == "__main__":
    main()
