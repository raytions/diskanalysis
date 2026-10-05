from pydantic_settings import BaseSettings
from pydantic import model_validator
from pathlib import Path

_DOCKER_DB = "/data/diskanalysis.db"
_LOCAL_DB = str(Path(__file__).parent.parent / "data" / "diskanalysis.db")
_IN_DOCKER = Path("/.dockerenv").exists()


class Settings(BaseSettings):
    APP_ENV: str = "production"
    APP_HOST: str = "::"
    APP_PORT: int = 8000
    DB_PATH: str = _DOCKER_DB
    DEFAULT_COLD_DAYS: int = 90
    DEFAULT_TOP_N: int = 100
    SCAN_ROOTS: str = ""
    EXCLUDE_PATHS: str = "/proc,/sys,/dev,/run,/snap"
    ALLOWED_FS_TYPES: str = ""
    NETWORK_FS_TYPES: str = "nfs,nfs4,cifs,smb3,smbfs,davfs,samba,fuse.sshfs,fuse.s3fs,glusterfs,cephfs,lustre"
    LOG_LEVEL: str = "INFO"
    ADMIN_PASSWORD: str = ""  # Leave empty to disable auth (open access)
    PATH_USER_ATTRIBUTION: bool = True  # Attribute files by top-level directory name before falling back to uid

    @model_validator(mode="after")
    def _fix_local_db_path(self) -> "Settings":
        """When not in Docker and DB_PATH is the Docker default, use the local project path."""
        if not _IN_DOCKER and self.DB_PATH == _DOCKER_DB:
            self.DB_PATH = _LOCAL_DB
        return self

    @property
    def exclude_paths_list(self) -> list[str]:
        return [p.strip() for p in self.EXCLUDE_PATHS.split(",") if p.strip()]

    @property
    def network_fs_types_set(self) -> set[str]:
        return {t.strip().lower() for t in self.NETWORK_FS_TYPES.split(",") if t.strip()}

    @property
    def allowed_fs_types_set(self) -> set[str]:
        if not self.ALLOWED_FS_TYPES.strip():
            return set()
        return {t.strip().lower() for t in self.ALLOWED_FS_TYPES.split(",") if t.strip()}

    @property
    def db_url(self) -> str:
        Path(self.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.DB_PATH}"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
