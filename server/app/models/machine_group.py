from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import relationship
from datetime import datetime

from ..core.database import Base


class MachineGroup(Base):
    __tablename__ = "machine_groups"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    machines = relationship("Machine", back_populates="group")
