from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    APP_NAME: str = "Bretter-IMG"
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    AGENT_TOKEN_EXPIRE_DAYS: int = 365
    AGENT_AUTO_UPDATE_ENABLED: bool = True

    DATABASE_URL: str = "sqlite:///./bretter.db"
    IMAGE_STORE_PATH: str = str(Path(__file__).parent.parent.parent.parent / "images-store")
    WINPE_STORE_PATH: str = str(Path(__file__).parent.parent.parent.parent / "winpe-store")
    JOB_PAYLOAD_STORE_PATH: str = str(Path(__file__).parent.parent.parent.parent / "job-payloads")

    SAMBA_AUTO_PROVISION_USERS: bool = True
    SAMBA_GROUP: str = "bretter-img"
    SAMBA_LOGIN_SHELL: str = "/usr/sbin/nologin"

    # How often agents should poll (seconds)
    AGENT_POLL_INTERVAL: int = 10

    class Config:
        env_file = ".env"


settings = Settings()
