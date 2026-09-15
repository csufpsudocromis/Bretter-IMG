from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
import uuid

from ..core.database import get_db
from ..core.security import (
    verify_password, hash_password, create_access_token,
    create_agent_token, get_current_user,
)
from ..models.user import User
from ..models.machine import Machine
from ..schemas.user import Token, UserCreate, UserCreatedOut, UserOut
from ..services.samba import (
    SambaProvisionError,
    generate_smb_password,
    normalize_username,
    provision_samba_access,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user.username, "type": "user"})
    return {"access_token": token, "token_type": "bearer"}


@router.post("/register", response_model=UserCreatedOut)
def register(body: UserCreate, db: Session = Depends(get_db)):
    try:
        username = normalize_username(body.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    first_user = db.query(User).count() == 0
    smb_password = generate_smb_password()
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        hashed_password=hash_password(body.password),
        is_admin=first_user,
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


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/refresh", response_model=Token)
def refresh_session(current_user: User = Depends(get_current_user)):
    token = create_access_token({"sub": current_user.username, "type": "user"})
    return {"access_token": token, "token_type": "bearer"}


@router.post("/agent-token/{machine_id}", response_model=Token)
def issue_agent_token(
    machine_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    token = create_agent_token(machine_id)
    return {"access_token": token, "token_type": "bearer"}
