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


def _resolve_users(uid_rows) -> tuple[dict, set, dict, int, int]:
    """
    Resolve uid rows to usernames via stored value or pwd lookup.
    Returns (agg, known_usernames, unresolved_uids, null_fc, null_ts).
    agg: defaultdict {username -> {file_count, total_size_bytes, uids}}
    unresolved_uids: {uid -> {file_count, total_size_bytes}} for uids that failed pwd lookup
    null_fc/null_ts: totals for files with NULL uid
    """
    import pwd
    import re
    from collections import defaultdict

    _SAFE_NAME = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
    agg = defaultdict(lambda: {"file_count": 0, "total_size_bytes": 0, "uids": set()})
    known_usernames: set[str] = set()
    unresolved: dict[int, dict] = {}
    null_fc = null_ts = 0

    for uid, stored_name, fc, ts in uid_rows:
        fc = fc or 0
        ts = ts or 0
        if stored_name and _SAFE_NAME.match(stored_name):
            name = stored_name
            known_usernames.add(name)
            agg[name]["file_count"] += fc
            agg[name]["total_size_bytes"] += ts
            if uid is not None:
                agg[name]["uids"].add(str(uid))
        elif uid is not None:
            try:
                name = pwd.getpwuid(uid).pw_name
                if _SAFE_NAME.match(name):
                    known_usernames.add(name)
                    agg[name]["file_count"] += fc
                    agg[name]["total_size_bytes"] += ts
                    agg[name]["uids"].add(str(uid))
                else:
                    unresolved[uid] = {"file_count": fc, "total_size_bytes": ts}
            except (KeyError, TypeError):
                unresolved[uid] = {"file_count": fc, "total_size_bytes": ts}
        else:
            null_fc += fc
            null_ts += ts

    return agg, known_usernames, unresolved, null_fc, null_ts


def _apply_path_attribution(conn, where_sql: str, agg: dict, known_usernames: set,
                             unresolved: dict, null_fc: int, null_ts: int) -> None:
    """
    For files with unresolvable uid or NULL uid, attribute to a known username
    if the file path contains that username as a path component.
    Deducts from the uid:N/unknown bucket and adds to the matched username.
    Modifies agg in-place.
    """
    import re
    from collections import defaultdict
    from sqlalchemy import text as sqlt

    _SAFE_NAME = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
    has_unattr = bool(unresolved) or null_fc > 0

    if not known_usernames or not has_unattr:
        # No path matching needed — just flush remainders directly
        for uid, totals in unresolved.items():
            agg[f"uid:{uid}"]["file_count"] += totals["file_count"]
            agg[f"uid:{uid}"]["total_size_bytes"] += totals["total_size_bytes"]
            agg[f"uid:{uid}"]["uids"].add(str(uid))
        if null_fc > 0:
            agg["unknown"]["file_count"] += null_fc
            agg["unknown"]["total_size_bytes"] += null_ts
        return

    # Build CASE WHEN — longer names first to avoid prefix collisions (e.g. "yang" vs "ltyang")
    sorted_names = sorted((n for n in known_usernames if _SAFE_NAME.match(n)), key=len, reverse=True)
    case_parts = " ".join(
        f"WHEN path LIKE '%/{n}/%' OR path LIKE '%/{n}' THEN '{n}'"
        for n in sorted_names
    )

    uid_filter = "uid IS NULL"
    if unresolved:
        uid_list = ",".join(str(u) for u in unresolved)
        uid_filter = f"(uid IS NULL OR uid IN ({uid_list}))"

    path_sql = f"""
        SELECT uid,
               CASE {case_parts} ELSE NULL END AS matched_user,
               SUM(size_bytes) AS total_size_bytes,
               COUNT(*) AS file_count
        FROM files
        WHERE {where_sql} AND {uid_filter}
        GROUP BY uid, matched_user
    """
    rows = conn.execute(sqlt(path_sql)).fetchall()

    uid_matched: dict = defaultdict(lambda: {"file_count": 0, "total_size_bytes": 0})
    for row_uid, matched_user, ts, fc in rows:
        if matched_user is not None:
            agg[matched_user]["file_count"] += fc or 0
            agg[matched_user]["total_size_bytes"] += ts or 0
            uid_matched[row_uid]["file_count"] += fc or 0
            uid_matched[row_uid]["total_size_bytes"] += ts or 0

    # Remainder of unresolved uids that didn't match any path → uid:N
    for uid, totals in unresolved.items():
        m = uid_matched.get(uid, {"file_count": 0, "total_size_bytes": 0})
        rem_fc = totals["file_count"] - m["file_count"]
        rem_ts = totals["total_size_bytes"] - m["total_size_bytes"]
        if rem_fc > 0 or rem_ts > 0:
            agg[f"uid:{uid}"]["file_count"] += rem_fc
            agg[f"uid:{uid}"]["total_size_bytes"] += rem_ts
            agg[f"uid:{uid}"]["uids"].add(str(uid))

    # Remainder of NULL uid files that didn't match any path → unknown
    if null_fc > 0:
        m = uid_matched.get(None, {"file_count": 0, "total_size_bytes": 0})
        rem_fc = null_fc - m["file_count"]
        rem_ts = null_ts - m["total_size_bytes"]
        if rem_fc > 0:
            agg["unknown"]["file_count"] += rem_fc
            agg["unknown"]["total_size_bytes"] += rem_ts


def get_user_summary(
    db: Session,
    mount_id: int,
    limit: int = 50,
    scan_id: int | None = None,
) -> list[dict]:
    from sqlalchemy import text as sqlt

    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return []

    uid_sql = f"""
        SELECT uid, MAX(username) AS stored_username,
               COUNT(*) AS file_count, SUM(size_bytes) AS total_size_bytes
        FROM files
        WHERE mount_id = {mount_id} AND scan_id = {scan_id} AND is_symlink = 0
        GROUP BY uid
    """
    with db.bind.connect() as conn:
        uid_rows = conn.execute(sqlt(uid_sql)).fetchall()

    agg, known_usernames, unresolved, null_fc, null_ts = _resolve_users(uid_rows)

    where_sql = f"mount_id = {mount_id} AND scan_id = {scan_id} AND is_symlink = 0"
    with db.bind.connect() as conn:
        _apply_path_attribution(conn, where_sql, agg, known_usernames, unresolved, null_fc, null_ts)

    result = sorted(
        [{"uid": ",".join(sorted(v["uids"], key=lambda x: int(x) if x.isdigit() else 0)) or "—",
          "username": n,
          "file_count": v["file_count"],
          "total_size_bytes": v["total_size_bytes"]}
         for n, v in agg.items()],
        key=lambda x: -x["total_size_bytes"],
    )
    return result[:limit]


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
