from sqlalchemy import Column, String, DateTime, BigInteger
from datetime import datetime
from ..core.database import Base


class Image(Base):
    __tablename__ = "images"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    filename = Column(String, nullable=False)      # actual file on disk
    size_bytes = Column(BigInteger, default=0)
    os_version = Column(String)
    architecture = Column(String, default="x64")
    checksum_sha256 = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String)
    source_machine_id = Column(String)             # which machine it was captured from
    tags = Column(String)                          # comma-separated tags
