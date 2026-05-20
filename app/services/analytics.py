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


def get_path_attribution_enabled(conn) -> bool:
    """Read path_user_attribution toggle from app_settings table, fall back to config."""
    try:
        from sqlalchemy import text as sqlt
        row = conn.execute(sqlt(
            "SELECT value FROM app_settings WHERE key='path_user_attribution'"
        )).fetchone()
        if row is not None:
            return str(row[0]).lower() in ('true', '1', 'yes')
    except Exception:
        pass
    from app.config import settings
    return settings.PATH_USER_ATTRIBUTION


def _collect_known_usernames(conn, where_sql: str) -> tuple[set, dict]:
    """
    Query distinct uids in the file set, resolve to real usernames.
    Also includes names from the custom_users table.
    Returns (known_usernames, uid_to_name).
    known_usernames: safe real names used for path prefix matching.
    uid_to_name: uid -> resolved name (including "uid:N" fallbacks).
    """
    import pwd
    import re
    from sqlalchemy import text as sqlt
    _SAFE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')

    rows = conn.execute(sqlt(
        f"SELECT uid, MAX(username) FROM files WHERE {where_sql} GROUP BY uid"
    )).fetchall()

    known: set[str] = set()
    uid_to_name: dict = {}

    for uid, stored_name in rows:
        if stored_name and _SAFE.match(stored_name):
            known.add(stored_name)
            if uid is not None:
                uid_to_name[uid] = stored_name
        elif uid is not None:
            try:
                name = pwd.getpwuid(uid).pw_name
                if _SAFE.match(name):
                    known.add(name)
                    uid_to_name[uid] = name
                else:
                    uid_to_name[uid] = f"uid:{uid}"
            except (KeyError, TypeError):
                uid_to_name[uid] = f"uid:{uid}"

    # Also include manually curated custom users from settings
    try:
        cu_rows = conn.execute(sqlt("SELECT username FROM custom_users")).fetchall()
        for (name,) in cu_rows:
            if name and _SAFE.match(name):
                known.add(name)
    except Exception:
        pass

    return known, uid_to_name


def _build_user_agg_per_mount(conn, mount_triples: list, known_usernames: set, uid_to_name: dict, path_attribution: bool = True) -> dict:
    """
    Attribute disk usage to users using a two-step strategy per mount:

    1. Directory-based path attribution (PRIMARY, fast):
       Fetch top-level directories under each mount point from the *directories* table
       (cumulative sizes). Prefix-match the directory name against known usernames.
       Example: /media/NAS02/jiahao/ → jiahao, /media/hdd/yyfangspx/ → yyfang.
       This handles NFS4 mounts where uid isolation is absent — the folder name is
       authoritative regardless of file uid (e.g. uid=1024 colliding with local 'ula').

    2. Uid-based attribution (FALLBACK):
       Files not covered by any matched top-level directory are attributed by uid.
       Distributed proportionally based on each uid's share of the mount's total size.

    mount_triples: [(mount_id, scan_id, mount_point), ...]
    """
    import re
    from collections import defaultdict
    from sqlalchemy import text as sqlt
    _SAFE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')

    agg: dict = defaultdict(lambda: {"file_count": 0, "total_size_bytes": 0, "uids": set()})
    sorted_names = sorted((n for n in known_usernames if _SAFE.match(n)), key=len, reverse=True)

    for mount_id, scan_id, mount_point in mount_triples:
        # rstrip('/') makes mount_point='/' → off=2 work correctly (avoids off-by-one)
        off = len(mount_point.rstrip('/')) + 2

        # ── Step 1: top-level directory path attribution (optional) ──────────────────
        matched_size = matched_count = 0
        if path_attribution:
            # Primary: directories table (cumulative sizes, fast).
            dir_rows = conn.execute(sqlt(f"""
                SELECT SUBSTR(path, {off}) AS comp, total_size_bytes, file_count
                FROM directories
                WHERE mount_id = {mount_id} AND scan_id = {scan_id}
                AND SUBSTR(path, {off}) != ''
                AND INSTR(SUBSTR(path, {off}), '/') = 0
            """)).fetchall()

            # Fallback: directories table empty for this mount (e.g. interrupted scan).
            # Derive first-component totals directly from the files table instead.
            if not dir_rows:
                dir_rows = conn.execute(sqlt(f"""
                    SELECT
                        CASE
                            WHEN INSTR(SUBSTR(path, {off}), '/') = 0 THEN SUBSTR(path, {off})
                            ELSE SUBSTR(SUBSTR(path, {off}), 1, INSTR(SUBSTR(path, {off}), '/') - 1)
                        END AS comp,
                        SUM(size_bytes) AS ts, COUNT(*) AS fc
                    FROM files
                    WHERE mount_id = {mount_id} AND scan_id = {scan_id}
                    GROUP BY comp
                """)).fetchall()

            for comp, ts, fc in dir_rows:
                if not comp:
                    continue
                ts = ts or 0
                fc = fc or 0
                # Only attribute to a bucket if the directory name prefix-matches a
                # known username (local passwd + custom_users list). Unmatched dirs
                # (datasets, shared dirs, etc.) fall through to the uid fallback below.
                matched_name = None
                for name in sorted_names:
                    if comp.lower().startswith(name.lower()):
                        matched_name = name
                        break
                if matched_name:
                    agg[matched_name]["file_count"] += fc
                    agg[matched_name]["total_size_bytes"] += ts
                    matched_size += ts
                    matched_count += fc

        # ── Step 2: uid-based attribution for unmatched portion ───────────────────────
        # Get uid totals for the whole mount (fast: GROUP BY uid with covering index).
        uid_rows = conn.execute(sqlt(f"""
            SELECT uid, SUM(size_bytes) AS ts, COUNT(*) AS fc
            FROM files WHERE mount_id = {mount_id} AND scan_id = {scan_id}
            GROUP BY uid
        """)).fetchall()

        mount_total_size = sum(r[1] or 0 for r in uid_rows)
        mount_total_count = sum(r[2] or 0 for r in uid_rows)
        unmatched_size = max(0, mount_total_size - matched_size)
        unmatched_count = max(0, mount_total_count - matched_count)

        if unmatched_count > 0 and mount_total_count > 0:
            for uid, ts, fc in uid_rows:
                ts = ts or 0
                fc = fc or 0
                name = uid_to_name.get(uid, f"uid:{uid}" if uid is not None else "unknown")
                frac_count = fc / mount_total_count
                frac_size = ts / mount_total_size if mount_total_size else frac_count
                agg[name]["file_count"] += round(unmatched_count * frac_count)
                agg[name]["total_size_bytes"] += round(unmatched_size * frac_size)
                if uid is not None and name in known_usernames:
                    agg[name]["uids"].add(str(uid))

    return agg


def get_user_summary(
    db: Session,
    mount_id: int,
    limit: int = 50,
    scan_id: int | None = None,
) -> list[dict]:
    from app.models import Mount as MountModel
    if scan_id is None:
        scan_id = _latest_scan_id_for_mount(db, mount_id)
    if scan_id is None:
        return []

    mount = db.query(MountModel).filter(MountModel.id == mount_id).first()
    mount_point = mount.mount_point if mount else ""

    where_sql = f"mount_id = {mount_id} AND scan_id = {scan_id}"
    with db.bind.connect() as conn:
        path_attr = get_path_attribution_enabled(conn)
        known_usernames, uid_to_name = _collect_known_usernames(conn, where_sql)
        agg = _build_user_agg_per_mount(conn, [(mount_id, scan_id, mount_point)], known_usernames, uid_to_name,
                                        path_attribution=path_attr)

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
