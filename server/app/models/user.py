from sqlalchemy import Column, String, DateTime, Boolean
from datetime import datetime
from ..core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    smb_username = Column(String, unique=True, index=True)
    smb_password = Column(String)
    smb_access_enabled = Column(Boolean, default=False)
    smb_provisioned_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
