from datetime import datetime
from sqlalchemy import (
    Integer, String, Boolean, Float, BigInteger,
    DateTime, Text, ForeignKey, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class Mount(Base):
    __tablename__ = "mounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mount_point: Mapped[str] = mapped_column(String, unique=True, index=True)
    device: Mapped[str] = mapped_column(String, default="")
    fs_type: Mapped[str] = mapped_column(String, default="")
    is_network_fs: Mapped[bool] = mapped_column(Boolean, default=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    free_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_percent: Mapped[float] = mapped_column(Float, default=0.0)
    is_accessible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    scans: Mapped[list["Scan"]] = relationship("Scan", back_populates="mount", cascade="all, delete-orphan")
    files: Mapped[list["File"]] = relationship("File", back_populates="mount", cascade="all, delete-orphan")
    directories: Mapped[list["Directory"]] = relationship("Directory", back_populates="mount", cascade="all, delete-orphan")


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mount_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("mounts.id"), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running | completed | failed
    scan_mode: Mapped[str] = mapped_column(String)  # all_mounts | single_mount | path
    target_path: Mapped[str | None] = mapped_column(String, nullable=True)
    filesystem_boundary_only: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    cold_days: Mapped[int] = mapped_column(Integer, default=90)
    top_n: Mapped[int] = mapped_column(Integer, default=100)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    total_dirs: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    cold_file_count: Mapped[int] = mapped_column(Integer, default=0)
    cold_total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    mount: Mapped["Mount | None"] = relationship("Mount", back_populates="scans")
    files: Mapped[list["File"]] = relationship("File", back_populates="scan", cascade="all, delete-orphan")
    directories: Mapped[list["Directory"]] = relationship("Directory", back_populates="scan", cascade="all, delete-orphan")
    errors: Mapped[list["ScanError"]] = relationship("ScanError", back_populates="scan", cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_scan_size", "scan_id", "size_bytes"),
        Index("ix_files_scan_atime", "scan_id", "atime"),
        Index("ix_files_mount_cold", "mount_id", "is_cold"),
        Index("ix_files_scan_cold", "scan_id", "is_cold"),
        Index("ix_files_mount_scan", "mount_id", "scan_id"),
        Index("ix_files_scan_uid_size", "scan_id", "uid", "size_bytes"),
        Index("ix_files_scan_username", "scan_id", "username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("scans.id"), index=True)
    mount_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("mounts.id"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    extension: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    atime: Mapped[float | None] = mapped_column(Float, nullable=True)
    mtime: Mapped[float | None] = mapped_column(Float, nullable=True)
    ctime: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_symlink: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cold: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cold_days_bucket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)

    scan: Mapped["Scan"] = relationship("Scan", back_populates="files")
    mount: Mapped["Mount | None"] = relationship("Mount", back_populates="files")


class Directory(Base):
    __tablename__ = "directories"
    __table_args__ = (
        Index("ix_dirs_scan_total_size", "scan_id", "total_size_bytes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("scans.id"), index=True)
    mount_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("mounts.id"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    cold_file_count: Mapped[int] = mapped_column(Integer, default=0)
    cold_total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    scan: Mapped["Scan"] = relationship("Scan", back_populates="directories")
    mount: Mapped["Mount | None"] = relationship("Mount", back_populates="directories")


class CustomUser(Base):
    __tablename__ = "custom_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    aliases: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ScanError(Base):
    __tablename__ = "scan_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("scans.id"), index=True)
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    error_type: Mapped[str] = mapped_column(String)
    error_message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    scan: Mapped["Scan"] = relationship("Scan", back_populates="errors")
