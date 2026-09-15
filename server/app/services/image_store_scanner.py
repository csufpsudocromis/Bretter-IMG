"""
Background service: image store scanner.

Scans the IMAGE_STORE_PATH directory every SCAN_INTERVAL seconds and
auto-registers any .wim / .img / .esd files that are on disk but are not
yet present in the images table.

This covers the case where WinPE (bootable USB capture) writes an image
directly to the SMB share without the agent calling agent-register-direct.
"""

import logging
import os
import hashlib
import threading
import uuid
from datetime import datetime

from ..core.config import settings
from ..core.database import SessionLocal
from ..models.image import Image

log = logging.getLogger(__name__)

SCAN_INTERVAL = 30          # seconds between scans
IMAGE_EXTENSIONS = {".wim", ".img", ".esd"}
MIN_VALID_BYTES = 1024 * 1024   # 1 MB – ignore tiny/partial files


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_once(store: str) -> None:
    """Scan the store directory and register any untracked image files."""
    if not os.path.isdir(store):
        return

    db = SessionLocal()
    try:
        # Build a set of filenames already in the DB
        known: set[str] = {row[0] for row in db.query(Image.filename).all()}

        for entry in os.scandir(store):
            if not entry.is_file(follow_symlinks=False):
                continue
            _, ext = os.path.splitext(entry.name)
            if ext.lower() not in IMAGE_EXTENSIONS:
                continue
            # Skip partial/in-progress files
            if ".partial" in entry.name.lower() or ".uploading" in entry.name.lower():
                continue
            if entry.name in known:
                continue

            try:
                size = entry.stat().st_size
            except OSError:
                continue

            if size < MIN_VALID_BYTES:
                log.debug("image_store_scanner: skipping small file %s (%d bytes)", entry.name, size)
                continue

            # Auto-register
            try:
                stem, _ = os.path.splitext(entry.name)
                name = stem.replace("_", " ").replace("-", " ").strip()
                checksum = _sha256(entry.path)
                image = Image(
                    id=str(uuid.uuid4()),
                    name=name or entry.name,
                    filename=entry.name,
                    size_bytes=size,
                    checksum_sha256=checksum,
                    created_by="auto-detected",
                    created_at=datetime.utcnow(),
                )
                db.add(image)
                db.commit()
                log.info("image_store_scanner: auto-registered %s (%d MB)", entry.name, size // (1024 * 1024))
                known.add(entry.name)
            except Exception:
                db.rollback()
                log.exception("image_store_scanner: failed to register %s", entry.name)
    finally:
        db.close()


def _scanner_loop(store: str) -> None:
    log.info("image_store_scanner: started, watching %s every %ds", store, SCAN_INTERVAL)
    import time
    while True:
        try:
            _scan_once(store)
        except Exception:
            log.exception("image_store_scanner: unexpected error during scan")
        time.sleep(SCAN_INTERVAL)


def start_image_store_scanner() -> None:
    """Spawn the scanner as a daemon thread (called once at app startup)."""
    store = settings.IMAGE_STORE_PATH
    t = threading.Thread(
        target=_scanner_loop,
        args=(store,),
        name="image-store-scanner",
        daemon=True,
    )
    t.start()
