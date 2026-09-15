from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool
    smb_username: Optional[str] = None
    smb_access_enabled: bool = False
    smb_provisioned_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreatedOut(UserOut):
    smb_password: Optional[str] = None
