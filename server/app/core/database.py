from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from ..models import machine_group, machine, image, job, user  # noqa: F401 — register models
    Base.metadata.create_all(bind=engine)
    migrate_schema()


def _ensure_column(table_name: str, column_name: str, ddl: str):
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


def migrate_schema():
    _ensure_column("users", "smb_username", "smb_username VARCHAR")
    _ensure_column("users", "smb_password", "smb_password VARCHAR")
    _ensure_column("users", "smb_access_enabled", "smb_access_enabled BOOLEAN DEFAULT 0")
    _ensure_column("users", "smb_provisioned_at", "smb_provisioned_at DATETIME")
    _ensure_column("images", "created_by", "created_by VARCHAR")
    _ensure_column("machines", "group_id", "group_id VARCHAR")
    _ensure_column("jobs", "capture_smb_username", "capture_smb_username VARCHAR")
    _ensure_column("jobs", "capture_smb_password", "capture_smb_password VARCHAR")
    _ensure_column("jobs", "progress_percent", "progress_percent INTEGER")
    _ensure_column("jobs", "remove_winpe_after_deploy", "remove_winpe_after_deploy VARCHAR DEFAULT 'false'")
    _ensure_column("jobs", "restore_hostname_after_deploy", "restore_hostname_after_deploy VARCHAR DEFAULT 'false'")
    _ensure_column("jobs", "original_hostname", "original_hostname VARCHAR")
    _ensure_column("jobs", "domain_name", "domain_name VARCHAR")
    _ensure_column("jobs", "domain_username", "domain_username VARCHAR")
    _ensure_column("jobs", "domain_password", "domain_password VARCHAR")
    _ensure_column("jobs", "sysprep_before_capture", "sysprep_before_capture VARCHAR DEFAULT 'false'")
    _ensure_column("jobs", "unattended_file_path", "unattended_file_path VARCHAR")
    _ensure_column("jobs", "parent_job_id", "parent_job_id VARCHAR")
    _ensure_column("jobs", "command_shell", "command_shell VARCHAR")
    _ensure_column("jobs", "command_text", "command_text TEXT")
    _ensure_column("jobs", "command_timeout_seconds", "command_timeout_seconds INTEGER")
    _ensure_column("jobs", "command_interactive", "command_interactive VARCHAR DEFAULT 'false'")
    _ensure_column("jobs", "transfer_target_path", "transfer_target_path VARCHAR")
    _ensure_column("jobs", "transfer_payload_name", "transfer_payload_name VARCHAR")
    _ensure_column("jobs", "transfer_extract_archive", "transfer_extract_archive VARCHAR DEFAULT 'true'")
