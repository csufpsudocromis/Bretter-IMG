from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import uuid

from ..core.database import get_db
from ..core.security import get_current_admin_user, hash_password
from ..models.user import User
from ..schemas.user import UserCreate, UserCreatedOut, UserOut
from ..services.samba import (
    SambaProvisionError,
    generate_smb_password,
    normalize_username,
    provision_samba_access,
)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user),
):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/", response_model=UserCreatedOut)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user),
):
    try:
        username = normalize_username(body.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    smb_password = generate_smb_password()
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        hashed_password=hash_password(body.password),
        is_admin=body.is_admin,
        smb_username=username,
        smb_password=smb_password,
    )
    try:
        provision_samba_access(user, smb_password)
    except (SambaProvisionError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not provision SMB access: {exc}")

    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        **UserOut.model_validate(user).model_dump(),
        "smb_password": smb_password,
    }


@router.post("/{user_id}/share-access", response_model=UserCreatedOut)
def reset_share_access(
    user_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    smb_password = generate_smb_password()
    try:
        user.smb_username = user.smb_username or normalize_username(user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user.smb_password = smb_password
    try:
        provision_samba_access(user, smb_password)
    except (SambaProvisionError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not provision SMB access: {exc}")
    db.commit()
    db.refresh(user)
    return {
        **UserOut.model_validate(user).model_dump(),
        "smb_password": smb_password,
    }
