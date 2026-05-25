"""
Analytics queries against scanned data.

All functions accept an open SQLAlchemy Session and return plain Python
structures (dicts / lists) for easy JSON serialisation.
"""
import logging
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.models import Directory, File, Scan, ScanError

logger = logging.getLogger(__name__)


def _latest_scan_id_for_mount(db: Session, mount_id: int) -> int | None:
    # Prefer completed, then stopped, then interrupted — all on scans table (never files).
    row = (
        db.query(Scan.id)
        .filter(
            Scan.mount_id == mount_id,
            Scan.status.in_(["completed", "stopped", "interrupted"]),
        )
        .order_by(
            case(
                (Scan.status == "completed", 0),
                (Scan.status == "stopped", 1),
                else_=2,
            ),
            Scan.id.desc(),
        )
        .first()
    )
    return row[0] if row else None


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


def _settings_hash(conn) -> str:
    """MD5 of current attribution settings (custom_users list + path_attr toggle).
    Used as a cache key — changes whenever users or the toggle change."""
    import hashlib
    from sqlalchemy import text as sqlt
    rows = conn.execute(sqlt(
        "SELECT username, aliases FROM custom_users ORDER BY username"
    )).fetchall()
    path_attr = get_path_attribution_enabled(conn)
    content = f"path_attr={path_attr}|" + ";".join(f"{u}:{a or ''}" for u, a in rows)
    return hashlib.md5(content.encode()).hexdigest()[:16]


def _cache_lookup(conn, mount_id: int, scan_id: int, settings_hash: str) -> list[dict] | None:
    from sqlalchemy import text as sqlt
    rows = conn.execute(sqlt(
        "SELECT username, uids, file_count, total_size_bytes FROM user_attribution_cache "
        "WHERE mount_id=:m AND scan_id=:s AND settings_hash=:h "
        "ORDER BY total_size_bytes DESC"
    ), {"m": mount_id, "s": scan_id, "h": settings_hash}).fetchall()
    if not rows:
        return None
    return [{"uid": r[1] or "—", "username": r[0], "file_count": r[2], "total_size_bytes": r[3]}
            for r in rows]


def _cache_store(conn, mount_id: int, scan_id: int, settings_hash: str, result: list[dict]) -> None:
    from sqlalchemy import text as sqlt
    # Replace any previous cache for this (mount, scan) — old settings_hash rows are stale
    conn.execute(sqlt(
        "DELETE FROM user_attribution_cache WHERE mount_id=:m AND scan_id=:s"
    ), {"m": mount_id, "s": scan_id})
    for row in result:
        conn.execute(sqlt(
            "INSERT INTO user_attribution_cache "
            "(mount_id, scan_id, settings_hash, username, uids, file_count, total_size_bytes) "
            "VALUES (:m, :s, :h, :u, :ids, :fc, :ts)"
        ), {"m": mount_id, "s": scan_id, "h": settings_hash,
            "u": row["username"], "ids": row.get("uid", "") or "",
            "fc": row["file_count"], "ts": row["total_size_bytes"]})
    conn.commit()


def invalidate_user_attribution_cache(conn, mount_id: int | None = None) -> None:
    """Call after custom_users changes or path_attr toggle changes."""
    from sqlalchemy import text as sqlt
    if mount_id is not None:
        conn.execute(sqlt("DELETE FROM user_attribution_cache WHERE mount_id=:m"), {"m": mount_id})
    else:
        conn.execute(sqlt("DELETE FROM user_attribution_cache"))
    conn.commit()


def prewarm_user_cache(mount_ids: list[int]) -> None:
    """Launch a daemon thread to pre-warm user attribution cache for the given mounts."""
    import threading
    from app.db import SessionLocal

    def _run():
        db = SessionLocal()
        try:
            for mid in mount_ids:
                try:
                    get_user_summary(db, mid)
                    logger.info("Cache pre-warmed for mount %d", mid)
                except Exception as e:
                    logger.warning("Cache prewarm failed for mount %d: %s", mid, e)
        except Exception as e:
            logger.warning("Cache prewarm thread error: %s", e)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name="cache-prewarm").start()


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


def _collect_known_usernames(conn, where_sql: str) -> tuple[set, dict, dict]:
    """
    Query distinct uids in the file set, resolve to real usernames.
    Also includes names and aliases from the custom_users table.

    Returns (known_names, uid_to_canonical, name_to_canonical):
      known_names:      all primary names + aliases used for prefix matching
      uid_to_canonical: uid -> canonical (primary) username
      name_to_canonical: every name/alias -> its canonical primary username
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

    # Build canonical mapping: each name maps to itself initially
    name_to_canonical: dict = {n: n for n in known}

    # Load custom_users: primary names + their aliases
    try:
        cu_rows = conn.execute(sqlt("SELECT username, aliases FROM custom_users")).fetchall()
        for primary, aliases_str in cu_rows:
            if not primary or not _SAFE.match(primary):
                continue
            known.add(primary)
            name_to_canonical[primary] = primary
            if aliases_str:
                for alias in [a.strip() for a in aliases_str.split(',') if a.strip()]:
                    if _SAFE.match(alias):
                        known.add(alias)
                        name_to_canonical[alias] = primary  # alias resolves to primary
    except Exception:
        pass

    # Remap uid_to_name through canonical so uid lookups return primary names
    uid_to_canonical = {uid: name_to_canonical.get(n, n) for uid, n in uid_to_name.items()}

    return known, uid_to_canonical, name_to_canonical


def _build_user_agg_per_mount(conn, mount_triples: list, known_usernames: set, uid_to_name: dict, path_attribution: bool = True, name_to_canonical: dict | None = None) -> dict:
    """
    Attribute disk usage to users using a two-phase strategy per mount.

    Each file is counted exactly once:

    Step 1a — Depth-1 directory attribution (if enabled):
      Match immediate child directory names of the mount point against known usernames.
      The entire subtree (cumulative size from directories table) is credited to the
      matched user. Unmatched depth-1 dirs are passed to Step 1b.

    Step 1b — Depth-2 directory attribution (if enabled):
      For depth-1 dirs that didn't match any user (e.g. an intermediate "USER_PATH"
      directory), look one level deeper and match their immediate children against
      known usernames. This handles mounts where user directories are not at the top
      level, e.g. /mnt/NAS/USER_PATH/yyfang/.

    Step 2 — Uid attribution for unmatched files:
      Files not under any path-attributed directory are attributed by uid. Exclusion
      uses NOT IN (depth-1 comps) + NOT LIKE (depth-2 matched prefixes).

    mount_triples: [(mount_id, scan_id, mount_point), ...]
    """
    import re
    from collections import defaultdict
    from sqlalchemy import text as sqlt
    _SAFE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')

    agg: dict = defaultdict(lambda: {"file_count": 0, "total_size_bytes": 0, "uids": set()})
    sorted_names = sorted((n for n in known_usernames if _SAFE.match(n)), key=len, reverse=True)

    def _match_name(comp: str) -> str | None:
        for name in sorted_names:
            if comp.lower().startswith(name.lower()):
                return name
        return None

    def _bucket(name: str) -> str:
        return name_to_canonical.get(name, name) if name_to_canonical else name

    for mount_id, scan_id, mount_point in mount_triples:
        mount_root = mount_point.rstrip('/')
        off = len(mount_root) + 2
        first_comp_sql = (
            f"CASE WHEN INSTR(SUBSTR(path,{off}),'/')=0 "
            f"THEN SUBSTR(path,{off}) "
            f"ELSE SUBSTR(SUBSTR(path,{off}),1,INSTR(SUBSTR(path,{off}),'/')-1) END"
        )

        matched_depth1_comps: set[str] = set()
        matched_depth2_prefixes: list[str] = []  # full absolute paths of depth-2 matched dirs

        if path_attribution:
            # ── Step 1a: depth-1 directories ─────────────────────────────────
            d1_rows = conn.execute(sqlt(f"""
                SELECT SUBSTR(path, {off}) AS comp, total_size_bytes, file_count
                FROM directories
                WHERE mount_id={mount_id} AND scan_id={scan_id}
                AND SUBSTR(path,{off}) != ''
                AND INSTR(SUBSTR(path,{off}),'/') = 0
            """)).fetchall()

            if not d1_rows:
                # Fallback when directories table is empty (e.g. interrupted scan)
                d1_rows = conn.execute(sqlt(f"""
                    SELECT {first_comp_sql} AS comp,
                           SUM(size_bytes) AS ts, COUNT(*) AS fc
                    FROM files WHERE mount_id={mount_id} AND scan_id={scan_id}
                    GROUP BY comp
                """)).fetchall()

            unmatched_d1: list[str] = []
            for comp, ts, fc in d1_rows:
                if not comp:
                    continue
                matched = _match_name(comp)
                if matched:
                    agg[_bucket(matched)]["file_count"] += fc or 0
                    agg[_bucket(matched)]["total_size_bytes"] += ts or 0
                    matched_depth1_comps.add(comp)
                else:
                    unmatched_d1.append(comp)

            # ── Step 1b: depth-2 dirs under unmatched depth-1 dirs ───────────
            # Handles layouts like /mnt/NAS/USER_PATH/yyfang/ where USER_PATH is
            # an intermediate container directory, not a user directory.
            for parent_comp in unmatched_d1:
                parent_path = f"{mount_root}/{parent_comp}"
                parent_off = len(parent_path) + 2

                d2_rows = conn.execute(sqlt(f"""
                    SELECT SUBSTR(path, {parent_off}) AS comp, total_size_bytes, file_count
                    FROM directories
                    WHERE mount_id={mount_id} AND scan_id={scan_id}
                    AND path LIKE :prefix
                    AND SUBSTR(path, {parent_off}) != ''
                    AND INSTR(SUBSTR(path, {parent_off}), '/') = 0
                """), {"prefix": f"{parent_path}/%"}).fetchall()

                if not d2_rows:
                    # Fallback from files table when directories table is empty
                    second_comp_sql = (
                        f"CASE WHEN INSTR(SUBSTR(path,{parent_off}),'/')=0 "
                        f"THEN SUBSTR(path,{parent_off}) "
                        f"ELSE SUBSTR(SUBSTR(path,{parent_off}),1,INSTR(SUBSTR(path,{parent_off}),'/')-1) END"
                    )
                    d2_rows = conn.execute(sqlt(f"""
                        SELECT {second_comp_sql} AS comp, SUM(size_bytes), COUNT(*)
                        FROM files WHERE mount_id={mount_id} AND scan_id={scan_id}
                        AND path LIKE :prefix
                        GROUP BY comp
                    """), {"prefix": f"{parent_path}/%"}).fetchall()

                for comp2, ts2, fc2 in d2_rows:
                    if not comp2:
                        continue
                    matched = _match_name(comp2)
                    if matched:
                        agg[_bucket(matched)]["file_count"] += fc2 or 0
                        agg[_bucket(matched)]["total_size_bytes"] += ts2 or 0
                        matched_depth2_prefixes.append(f"{parent_path}/{comp2}")

        # ── Step 2: uid attribution for files not under any matched dir ───────
        if matched_depth1_comps or matched_depth2_prefixes:
            extra_conds = []
            if matched_depth1_comps:
                not_in = ",".join(f"'{c}'" for c in matched_depth1_comps)
                extra_conds.append(f"({first_comp_sql}) NOT IN ({not_in})")
            for prefix in matched_depth2_prefixes:
                safe = prefix.replace("'", "''")
                extra_conds.append(f"path NOT LIKE '{safe}/%'")
            uid_rows = conn.execute(sqlt(f"""
                SELECT uid, SUM(size_bytes) AS ts, COUNT(*) AS fc
                FROM files
                WHERE mount_id={mount_id} AND scan_id={scan_id}
                AND {" AND ".join(extra_conds)}
                GROUP BY uid
            """)).fetchall()
        else:
            uid_rows = conn.execute(sqlt(f"""
                SELECT uid, SUM(size_bytes) AS ts, COUNT(*) AS fc
                FROM files WHERE mount_id={mount_id} AND scan_id={scan_id}
                GROUP BY uid
            """)).fetchall()

        for uid, ts, fc in uid_rows:
            ts = ts or 0
            fc = fc or 0
            name = uid_to_name.get(uid, f"uid:{uid}" if uid is not None else "unknown")
            agg[name]["file_count"] += fc
            agg[name]["total_size_bytes"] += ts
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
        shash = _settings_hash(conn)
        cached = _cache_lookup(conn, mount_id, scan_id, shash)
        if cached is not None:
            return cached[:limit]

        path_attr = get_path_attribution_enabled(conn)
        known_usernames, uid_to_canonical, name_to_canonical = _collect_known_usernames(conn, where_sql)
        agg = _build_user_agg_per_mount(conn, [(mount_id, scan_id, mount_point)], known_usernames, uid_to_canonical,
                                        path_attribution=path_attr, name_to_canonical=name_to_canonical)

        result = sorted(
            [{"uid": ",".join(sorted(v["uids"], key=lambda x: int(x) if x.isdigit() else 0)) or "—",
              "username": n,
              "file_count": v["file_count"],
              "total_size_bytes": v["total_size_bytes"]}
             for n, v in agg.items()],
            key=lambda x: -x["total_size_bytes"],
        )
        result = result[:limit]
        _cache_store(conn, mount_id, scan_id, shash, result)
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

    # Batch lookup: 1 query on scans table only (never touches files table)
    mount_ids = [m.id for m in mounts]
    if mount_ids:
        ids_str = ",".join(str(i) for i in mount_ids)
        with db.bind.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT mount_id, id FROM (
                    SELECT mount_id, id,
                        ROW_NUMBER() OVER (
                            PARTITION BY mount_id
                            ORDER BY
                                CASE status WHEN 'completed' THEN 0
                                            WHEN 'stopped' THEN 1
                                            WHEN 'interrupted' THEN 2
                                            ELSE 3 END,
                                id DESC
                        ) AS rn
                    FROM scans
                    WHERE mount_id IN ({ids_str})
                      AND status IN ('completed','stopped','interrupted')
                ) WHERE rn=1
            """)).fetchall()
        latest_scan_ids = list({r[1] for r in rows})
    else:
        latest_scan_ids = []

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
