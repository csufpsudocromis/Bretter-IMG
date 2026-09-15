from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid, os, hashlib, shutil, re
from urllib.parse import quote
from pydantic import BaseModel

from ..core.database import get_db
from ..core.security import get_current_user, get_current_agent_machine_id
from ..core.config import settings
from ..models.image import Image
from ..models.job import Job
from ..models.user import User
from ..schemas.image import ImageOut, ImageUpdate

router = APIRouter(prefix="/api/images", tags=["images"])

STORE = settings.IMAGE_STORE_PATH
MIN_CAPTURE_IMAGE_BYTES = 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class DirectCaptureRegister(BaseModel):
    filename: str
    name: str
    job_id: Optional[str] = None
    os_version: Optional[str] = None


class ImageStorageOut(BaseModel):
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    image_store_used_bytes: int
    image_count: int


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_image_filename(name: str, ext: str = ".wim") -> str:
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


def _unique_image_filename(db: Session, desired_filename: str) -> str:
    stem, ext = os.path.splitext(desired_filename)
    candidate = desired_filename
    suffix = 2
    while (
        os.path.exists(os.path.join(STORE, candidate))
        or db.query(Image).filter(Image.filename == candidate).first()
    ):
        suffix_text = f"-{suffix}"
        candidate = f"{stem[: max(1, 180 - len(ext) - len(suffix_text))].rstrip(' .')}{suffix_text}{ext}"
        suffix += 1
    return candidate


def _save_upload_atomic(file: UploadFile, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    temp_dest = f"{dest}.{uuid.uuid4().hex}.uploading"
    try:
        with open(temp_dest, "wb") as out:
            shutil.copyfileobj(file.file, out)
        os.replace(temp_dest, dest)
    except Exception:
        if os.path.exists(temp_dest):
            os.remove(temp_dest)
        raise


def _existing_storage_path(path: str) -> str:
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return probe
        probe = parent
    return probe


def _directory_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return total
    for root, _, files in os.walk(path):
        for filename in files:
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                continue
    return total


def _image_download_path(image_id: str, db: Session) -> tuple[Image, str]:
    img = db.query(Image).filter(Image.id == image_id).first()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    path = os.path.join(STORE, img.filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Image file not found on disk")
    return img, path


def _download_headers(filename: str, size: int, content_length: int | None = None) -> dict[str, str]:
    safe_filename = os.path.basename(filename).replace('"', "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": (
            f"attachment; filename=\"{safe_filename}\"; "
            f"filename*=UTF-8''{quote(safe_filename)}"
        ),
        "Content-Type": "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
    }
    headers["Content-Length"] = str(content_length if content_length is not None else size)
    return headers


def _parse_range_header(range_header: str | None, size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="Invalid range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(
            status_code=416,
            detail="Invalid range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length == 0:
            raise HTTPException(
                status_code=416,
                detail="Invalid range",
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start = max(size - suffix_length, 0)
        end = size - 1
    if start >= size or end < start:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    return start, min(end, size - 1)


def _iter_file_range(path: str, start: int, end: int):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(DOWNLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.post("/upload", response_model=ImageOut)
async def upload_image(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    os_version: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_machine_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    image_id = str(uuid.uuid4())
    ext = os.path.splitext(file.filename)[1] or ".wim"
    filename = _unique_image_filename(db, _safe_image_filename(name, ext))
    dest = os.path.join(STORE, filename)
    _save_upload_atomic(file, dest)

    size = os.path.getsize(dest)
    checksum = _sha256(dest)

    image = Image(
        id=image_id, name=name, description=description,
        filename=filename, size_bytes=size, os_version=os_version,
        checksum_sha256=checksum, source_machine_id=source_machine_id, tags=tags,
        created_by=current_user.username,
    )
    db.add(image)
    db.commit()
    db.refresh(image)
    return image


@router.post("/agent-upload", response_model=ImageOut)
async def agent_upload_image(
    file: UploadFile = File(...),
    name: str = Form(...),
    os_version: Optional[str] = Form(None),
    job_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    machine_id: str = Depends(get_current_agent_machine_id),
):
    """Agents use this endpoint to push a captured WIM back to the server."""
    image_id = str(uuid.uuid4())
    ext = os.path.splitext(file.filename)[1] or ".wim"
    filename = _unique_image_filename(db, _safe_image_filename(name, ext))
    dest = os.path.join(STORE, filename)
    _save_upload_atomic(file, dest)

    size = os.path.getsize(dest)
    checksum = _sha256(dest)

    created_by = None
    if job_id:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job and job.machine_id == machine_id:
            created_by = job.created_by

    image = Image(
        id=image_id, name=name, filename=filename,
        size_bytes=size, os_version=os_version,
        checksum_sha256=checksum, source_machine_id=machine_id,
        created_by=created_by,
    )
    db.add(image)
    db.commit()
    db.refresh(image)
    return image


@router.post("/agent-register-direct", response_model=ImageOut)
def agent_register_direct_capture(
    body: DirectCaptureRegister,
    db: Session = Depends(get_db),
    machine_id: str = Depends(get_current_agent_machine_id),
):
    """Register a WIM that WinPE wrote directly into the server image store via SMB."""
    filename = os.path.basename(body.filename)
    if not filename or filename != body.filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if filename.lower().endswith(".partial.wim"):
        raise HTTPException(status_code=400, detail="Direct capture is still a partial working file")
    path = os.path.join(STORE, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Direct capture file not found in image store")
    size = os.path.getsize(path)
    if size < MIN_CAPTURE_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Direct capture file is too small to be a valid image")

    existing = db.query(Image).filter(Image.filename == filename).first()
    if existing:
        job = db.query(Job).filter(Job.id == body.job_id).first() if body.job_id else None
        if body.name:
            existing.name = body.name
        existing.size_bytes = size
        existing.os_version = body.os_version
        existing.checksum_sha256 = _sha256(path)
        existing.source_machine_id = machine_id
        if job and job.machine_id == machine_id:
            existing.created_by = job.created_by
        db.commit()
        db.refresh(existing)
        return existing

    desired_filename = _safe_image_filename(body.name, os.path.splitext(filename)[1] or ".wim")
    if filename != desired_filename:
        new_filename = _unique_image_filename(db, desired_filename)
        new_path = os.path.join(STORE, new_filename)
        os.replace(path, new_path)
        filename = new_filename
        path = new_path

    image_id = body.job_id or str(uuid.uuid4())
    if db.query(Image).filter(Image.id == image_id).first():
        image_id = str(uuid.uuid4())

    job = db.query(Job).filter(Job.id == body.job_id).first() if body.job_id else None
    created_by = job.created_by if job and job.machine_id == machine_id else None

    image = Image(
        id=image_id,
        name=body.name,
        filename=filename,
        size_bytes=size,
        os_version=body.os_version,
        checksum_sha256=_sha256(path),
        source_machine_id=machine_id,
        created_by=created_by,
    )
    db.add(image)
    db.commit()
    db.refresh(image)
    return image


@router.get("/", response_model=List[ImageOut])
def list_images(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Image).all()


@router.post("/scan", response_model=dict)
def scan_image_store(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Immediately scan the image store for unregistered image files and register them."""
    from ..services.image_store_scanner import _scan_once
    before = db.query(Image).count()
    _scan_once(STORE)
    after = db.query(Image).count()
    return {"registered": after - before, "total": after}


@router.get("/storage/status", response_model=ImageStorageOut)
def get_image_storage_status(db: Session = Depends(get_db), _=Depends(get_current_user)):
    disk = shutil.disk_usage(_existing_storage_path(STORE))
    return ImageStorageOut(
        path=os.path.abspath(STORE),
        total_bytes=disk.total,
        used_bytes=disk.used,
        free_bytes=disk.free,
        image_store_used_bytes=_directory_size(STORE),
        image_count=db.query(Image).count(),
    )


@router.get("/{image_id}", response_model=ImageOut)
def get_image(image_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    img = db.query(Image).filter(Image.id == image_id).first()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    return img


@router.head("/{image_id}/download")
def head_download_image(image_id: str, db: Session = Depends(get_db)):
    """Allow browsers/download managers to validate large image downloads."""
    img, path = _image_download_path(image_id, db)
    size = os.path.getsize(path)
    return Response(headers=_download_headers(img.filename, size))


@router.get("/{image_id}/download")
def download_image(image_id: str, request: Request, db: Session = Depends(get_db)):
    """Download an image with byte-range support for large WIM files."""
    img, path = _image_download_path(image_id, db)
    size = os.path.getsize(path)
    byte_range = _parse_range_header(request.headers.get("range"), size)
    if byte_range:
        start, end = byte_range
        length = end - start + 1
        headers = _download_headers(img.filename, size, content_length=length)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            _iter_file_range(path, start, end),
            status_code=206,
            media_type="application/octet-stream",
            headers=headers,
        )

    return StreamingResponse(
        _iter_file_range(path, 0, size - 1),
        media_type="application/octet-stream",
        headers=_download_headers(img.filename, size),
    )


@router.patch("/{image_id}", response_model=ImageOut)
def update_image(image_id: str, body: ImageUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    img = db.query(Image).filter(Image.id == image_id).first()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    updates = body.dict(exclude_unset=True)
    if "name" in updates:
        updates["name"] = (updates["name"] or "").strip()
        if not updates["name"]:
            raise HTTPException(status_code=400, detail="Image name is required")
    for field in ("description", "os_version", "tags"):
        if field in updates and updates[field] is not None:
            updates[field] = updates[field].strip() or None
    for field, value in updates.items():
        setattr(img, field, value)
    db.commit()
    db.refresh(img)
    return img


@router.delete("/{image_id}")
def delete_image(image_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    img = db.query(Image).filter(Image.id == image_id).first()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    path = os.path.join(STORE, img.filename)
    if os.path.exists(path):
        os.remove(path)
    db.delete(img)
    db.commit()
    return {"ok": True}
