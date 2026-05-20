"""
Analytics queries against scanned data.

All functions accept an open SQLAlchemy Session and return plain Python
structures (dicts / lists) for easy JSON serialisation.
"""
import logging
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models import Directory, File, Scan, ScanError

logger = logging.getLogger(__name__)


def _latest_scan_id_for_mount(db: Session, mount_id: int) -> int | None:
    # Prefer a mount-specific completed scan first
    row = (
        db.query(Scan.id)
        .filter(Scan.mount_id == mount_id, Scan.status == "completed")
        .order_by(Scan.finished_at.desc())
        .first()
    )
    if row:
        return row[0]
    # Fall back: find the most recent scan_id that has files for this mount.
    # Use MAX(scan_id) — fast with the ix_files_mount_scan index.
    max_scan_id = (
        db.query(func.max(File.scan_id))
        .filter(File.mount_id == mount_id)
        .scalar()
    )
    return max_scan_id


_FILE_SORT_COLS = {"size_bytes": File.size_bytes, "mtime": File.mtime, "atime": File.atime, "extension": File.extension, "path": File.path}
_DIR_SORT_COLS = {"total_size_bytes": Directory.total_size_bytes, "file_count": Directory.file_count, "cold_file_count": Directory.cold_file_count, "cold_total_size_bytes": Directory.cold_total_size_bytes, "path": Directory.path}


def get_largest_files(
    db: Session,
    mount_id: int,
    limit: int = 100,
    offset: int = 0,
    scan_id: int | None = None,
    search: str | None = None,
    sort_by: str = "size_bytes",
    sort_dir: str = "desc",
    count: bool = True,
) -> dict:
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return {"total": None, "files": []}
    q = db.query(File).filter(File.mount_id == mount_id, File.scan_id == scan_id, File.is_symlink == False)
    if search:
        q = q.filter(File.path.contains(search))
    col = _FILE_SORT_COLS.get(sort_by, File.size_bytes)
    q = q.order_by(col.asc() if sort_dir == "asc" else col.desc())
    total = q.count() if count else None
    rows = q.offset(offset).limit(limit).all()
    return {"total": total, "files": [_file_dict(r) for r in rows]}


def get_largest_directories(
    db: Session,
    mount_id: int,
    limit: int = 100,
    offset: int = 0,
    scan_id: int | None = None,
    search: str | None = None,
    sort_by: str = "total_size_bytes",
    sort_dir: str = "desc",
    count: bool = True,
) -> dict:
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return {"total": None, "dirs": []}
    q = db.query(Directory).filter(Directory.mount_id == mount_id, Directory.scan_id == scan_id)
    if search:
        q = q.filter(Directory.path.contains(search))
    col = _DIR_SORT_COLS.get(sort_by, Directory.total_size_bytes)
    q = q.order_by(col.asc() if sort_dir == "asc" else col.desc())
    total = q.count() if count else None
    rows = q.offset(offset).limit(limit).all()
    return {"total": total, "dirs": [_dir_dict(r) for r in rows]}


def get_cold_files(
    db: Session,
    mount_id: int,
    days: int = 90,
    limit: int = 100,
    offset: int = 0,
    path_prefix: str | None = None,
    scan_id: int | None = None,
    search: str | None = None,
    sort_by: str = "size_bytes",
    sort_dir: str = "desc",
) -> dict:
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return {"total_count": 0, "total_size_bytes": 0, "files": []}

    base = [
        File.mount_id == mount_id,
        File.scan_id == scan_id,
        File.cold_days_bucket >= days,
        File.is_symlink == False,
    ]
    if path_prefix:
        base.append(File.path.like(f"{path_prefix}%"))
    if search:
        base.append(File.path.contains(search))

    q = db.query(File).filter(*base)
    total_count = q.count()
    total_size = db.query(func.sum(File.size_bytes)).filter(*base).scalar() or 0
    col = _FILE_SORT_COLS.get(sort_by, File.size_bytes)
    files = q.order_by(col.asc() if sort_dir == "asc" else col.desc()).offset(offset).limit(limit).all()
    return {
        "total_count": total_count,
        "total_size_bytes": total_size,
        "files": [_file_dict(r) for r in files],
    }


def get_cold_directories(
    db: Session,
    mount_id: int,
    days: int = 90,
    limit: int = 100,
    offset: int = 0,
    scan_id: int | None = None,
) -> list[dict]:
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return []
    rows = (
        db.query(Directory)
        .filter(
            Directory.mount_id == mount_id,
            Directory.scan_id == scan_id,
            Directory.cold_file_count > 0,
        )
        .order_by(Directory.cold_total_size_bytes.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_dir_dict(r) for r in rows]


def get_extension_summary(
    db: Session,
    mount_id: int,
    limit: int = 50,
    scan_id: int | None = None,
) -> list[dict]:
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return []
    rows = (
        db.query(
            File.extension,
            func.count(File.id).label("file_count"),
            func.sum(File.size_bytes).label("total_size_bytes"),
        )
        .filter(File.mount_id == mount_id, File.scan_id == scan_id)
        .group_by(File.extension)
        .order_by(text("total_size_bytes DESC"))
        .limit(limit)
        .all()
    )
    return [
        {
            "extension": r.extension or "(none)",
            "file_count": r.file_count,
            "total_size_bytes": r.total_size_bytes or 0,
        }
        for r in rows
    ]


def get_user_summary(
    db: Session,
    mount_id: int,
    limit: int = 50,
    scan_id: int | None = None,
) -> list[dict]:
    import pwd
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return []
    rows = (
        db.query(
            File.uid,
            func.count(File.id).label("file_count"),
            func.sum(File.size_bytes).label("total_size_bytes"),
        )
        .filter(File.mount_id == mount_id, File.scan_id == scan_id, File.is_symlink == False)
        .group_by(File.uid)
        .order_by(text("total_size_bytes DESC"))
        .limit(limit)
        .all()
    )
    result = []
    for r in rows:
        uid = r.uid
        try:
            username = pwd.getpwuid(uid).pw_name if uid is not None else "unknown"
        except (KeyError, TypeError):
            username = f"uid:{uid}" if uid is not None else "unknown"
        result.append({
            "uid": uid,
            "username": username,
            "file_count": r.file_count,
            "total_size_bytes": r.total_size_bytes or 0,
        })
    return result


def get_dashboard_summary(db: Session) -> dict:
    from app.models import Mount
    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    total_bytes = sum(m.total_bytes for m in mounts)
    used_bytes = sum(m.used_bytes for m in mounts)
    free_bytes = sum(m.free_bytes for m in mounts)
    usage_percent = round(used_bytes / total_bytes * 100, 2) if total_bytes else 0.0

    top_mounts = sorted(mounts, key=lambda m: m.usage_percent, reverse=True)[:5]
    recent_scans = (
        db.query(Scan).order_by(Scan.started_at.desc()).limit(5).all()
    )

    # Collect per-mount latest scan IDs for true cross-mount aggregation
    latest_scan_ids = []
    for mount in mounts:
        sid = _latest_scan_id_for_mount(db, mount.id)
        if sid:
            latest_scan_ids.append(sid)

    if latest_scan_ids:
        latest_scans = db.query(Scan).filter(Scan.id.in_(latest_scan_ids)).all()
        cold_count = sum(s.cold_file_count or 0 for s in latest_scans)
        cold_total = sum(s.cold_total_size_bytes or 0 for s in latest_scans)
    else:
        cold_total = 0
        cold_count = 0

    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "usage_percent": usage_percent,
        "mount_count": len(mounts),
        "top_mounts": [_mount_dict(m) for m in top_mounts],
        "recent_scans": [_scan_dict(s) for s in recent_scans],
        "cold_total_size_bytes": cold_total,
        "cold_file_count": cold_count,
    }


def _file_dict(f: File) -> dict:
    return {
        "id": f.id,
        "path": f.path,
        "size_bytes": f.size_bytes,
        "extension": f.extension,
        "atime": f.atime,
        "mtime": f.mtime,
        "is_symlink": f.is_symlink,
        "is_cold": f.is_cold,
        "cold_days_bucket": f.cold_days_bucket,
        "scan_id": f.scan_id,
        "mount_id": f.mount_id,
    }


def _dir_dict(d: Directory) -> dict:
    return {
        "id": d.id,
        "path": d.path,
        "total_size_bytes": d.total_size_bytes,
        "file_count": d.file_count,
        "cold_file_count": d.cold_file_count,
        "cold_total_size_bytes": d.cold_total_size_bytes,
        "scan_id": d.scan_id,
        "mount_id": d.mount_id,
    }


def _mount_dict(m) -> dict:
    return {
        "id": m.id,
        "mount_point": m.mount_point,
        "fs_type": m.fs_type,
        "is_network_fs": m.is_network_fs,
        "total_bytes": m.total_bytes,
        "used_bytes": m.used_bytes,
        "free_bytes": m.free_bytes,
        "usage_percent": m.usage_percent,
        "is_accessible": m.is_accessible,
        "last_scanned_at": m.last_scanned_at.isoformat() if m.last_scanned_at else None,
    }


def _scan_dict(s: Scan) -> dict:
    return {
        "id": s.id,
        "scan_mode": s.scan_mode,
        "target_path": s.target_path,
        "status": s.status,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        "duration_seconds": s.duration_seconds,
        "total_files": s.total_files,
        "total_bytes": s.total_bytes,
        "error_count": s.error_count,
        "mount_id": s.mount_id,
    }
