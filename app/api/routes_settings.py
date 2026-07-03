import re
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List

from app.db import get_db
from app.schemas import ok
from app import auth
from app.services.analytics import invalidate_user_attribution_cache as _invalidate_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings")

_SAFE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
_SYSTEM_UID_MAX = 999  # uids <= this are considered system users


def get_max_parallel_scans(conn) -> int:
    """Read max_parallel_scans from app_settings; returns default 4 if not set."""
    row = conn.execute(text("SELECT value FROM app_settings WHERE key='max_parallel_scans'")).fetchone()
    try:
        return max(1, min(int(row[0]), 32)) if row else 4
    except (ValueError, TypeError):
        return 4


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute(text("SELECT value FROM app_settings WHERE key=:key"), {"key": key}).fetchone()
    return str(row[0]) if row else default


def get_bool_setting(conn, key: str, default: bool = False) -> bool:
    value = get_setting(conn, key, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(text(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (:key, :value)"
    ), {"key": key, "value": value})


def get_sudo_mode_enabled(conn) -> bool:
    return get_bool_setting(conn, "sudo_mode_enabled", False)


def get_scan_schedule(conn) -> dict:
    try:
        interval = max(1, min(int(get_setting(conn, "scan_schedule_interval_minutes", "360")), 10080))
    except ValueError:
        interval = 360
    try:
        cold_days = max(1, int(get_setting(conn, "scan_schedule_cold_days", "90")))
    except ValueError:
        cold_days = 90
    raw_mount_ids = get_setting(conn, "scan_schedule_mount_ids", "")
    mount_ids = []
    if raw_mount_ids.strip():
        try:
            mount_ids = [int(v) for v in raw_mount_ids.split(",") if v.strip()]
        except ValueError:
            mount_ids = []
    return {
        "enabled": get_bool_setting(conn, "scan_schedule_enabled", False),
        "interval_minutes": interval,
        "scan_mode": get_setting(conn, "scan_schedule_mode", "all_mounts"),
        "scan_type": get_setting(conn, "scan_schedule_scan_type", "full"),
        "mount_ids": mount_ids,
        "target_path": get_setting(conn, "scan_schedule_target_path", ""),
        "filesystem_boundary_only": get_bool_setting(conn, "scan_schedule_filesystem_boundary_only", True),
        "exclude_rules": get_setting(conn, "scan_schedule_exclude_rules", ""),
        "cold_days": cold_days,
        "next_run_at": get_setting(conn, "scan_schedule_next_run_at", ""),
        "last_started_at": get_setting(conn, "scan_schedule_last_started_at", ""),
    }


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    from app.services.analytics import get_path_attribution_enabled
    with db.bind.connect() as conn:
        path_attr = get_path_attribution_enabled(conn)
        count = conn.execute(text("SELECT COUNT(*) FROM custom_users")).fetchone()[0]
    return ok({"path_user_attribution": path_attr, "custom_user_count": count})


class SettingRequest(BaseModel):
    path_user_attribution: bool


@router.post("")
def update_settings(req: SettingRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    with db.bind.connect() as conn:
        conn.execute(text(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('path_user_attribution', :v)"
        ), {"v": "true" if req.path_user_attribution else "false"})
        _invalidate_cache(conn)
        conn.commit()
    return ok({"path_user_attribution": req.path_user_attribution})


# ── Scan settings ────────────────────────────────────────────────────────────

@router.get("/scan")
def get_scan_settings(db: Session = Depends(get_db)):
    with db.bind.connect() as conn:
        n = get_max_parallel_scans(conn)
    return ok({"max_parallel_scans": n})


class ScanSettingsRequest(BaseModel):
    max_parallel_scans: int


class SudoSettingsRequest(BaseModel):
    enabled: bool


class ScanScheduleRequest(BaseModel):
    enabled: bool
    interval_minutes: int = 360
    scan_mode: str = "all_mounts"
    scan_type: str = "full"
    mount_ids: list[int] = []
    target_path: str = ""
    filesystem_boundary_only: bool = True
    exclude_rules: str = ""
    cold_days: int = 90


@router.post("/scan")
def update_scan_settings(req: ScanSettingsRequest, request: Request,
                         db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    n = max(1, min(req.max_parallel_scans, 32))
    with db.bind.connect() as conn:
        set_setting(conn, "max_parallel_scans", str(n))
        conn.commit()
    return ok({"max_parallel_scans": n})


@router.post("/sudo")
def update_sudo_settings(req: SudoSettingsRequest, request: Request,
                         db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    with db.bind.connect() as conn:
        set_setting(conn, "sudo_mode_enabled", "true" if req.enabled else "false")
        conn.commit()
    return ok({"sudo_mode_enabled": req.enabled})


@router.post("/scan-schedule")
def update_scan_schedule(req: ScanScheduleRequest, request: Request,
                         db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    from datetime import datetime, timedelta

    mode = req.scan_mode if req.scan_mode in {"all_mounts", "selected_mounts", "path"} else "all_mounts"
    scan_type = req.scan_type if req.scan_type in {"full", "incremental"} else "full"
    interval = max(1, min(req.interval_minutes, 10080))
    cold_days = max(1, req.cold_days)
    mount_ids = ",".join(str(i) for i in req.mount_ids if i > 0)
    next_run = (datetime.now() + timedelta(minutes=interval)).isoformat(timespec="seconds")

    with db.bind.connect() as conn:
        set_setting(conn, "scan_schedule_enabled", "true" if req.enabled else "false")
        set_setting(conn, "scan_schedule_interval_minutes", str(interval))
        set_setting(conn, "scan_schedule_mode", mode)
        set_setting(conn, "scan_schedule_scan_type", scan_type)
        set_setting(conn, "scan_schedule_mount_ids", mount_ids)
        set_setting(conn, "scan_schedule_target_path", req.target_path.strip())
        set_setting(conn, "scan_schedule_filesystem_boundary_only", "true" if req.filesystem_boundary_only else "false")
        set_setting(conn, "scan_schedule_exclude_rules", req.exclude_rules.strip())
        set_setting(conn, "scan_schedule_cold_days", str(cold_days))
        set_setting(conn, "scan_schedule_next_run_at", next_run)
        conn.commit()

    with db.bind.connect() as conn:
        data = get_scan_schedule(conn)
    return ok(data)


# ── Confirmed users ───────────────────────────────────────────────────────────

@router.get("/users")
def list_custom_users(db: Session = Depends(get_db)):
    with db.bind.connect() as conn:
        rows = conn.execute(text(
            "SELECT username, aliases, created_at FROM custom_users ORDER BY username"
        )).fetchall()
    return ok({
        "items": [{"username": r[0], "aliases": [a.strip() for a in (r[1] or "").split(",") if a.strip()], "created_at": r[2]} for r in rows],
        "total": len(rows),
    })


class UserRequest(BaseModel):
    username: str


@router.post("/users")
def add_custom_user(req: UserRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    name = req.username.strip()
    if not name or not _SAFE.match(name):
        return JSONResponse({"ok": False, "error": "Invalid username — only letters, digits, _ - . allowed"}, status_code=400)
    try:
        with db.bind.connect() as conn:
            existing = conn.execute(text("SELECT 1 FROM custom_users WHERE username=:u"), {"u": name}).fetchone()
            if existing:
                return JSONResponse({"ok": False, "error": "User already exists"}, status_code=409)
            conn.execute(text(
                "INSERT INTO custom_users (username, aliases, created_at) VALUES (:u, '', datetime('now'))"
            ), {"u": name})
            _invalidate_cache(conn)
            conn.commit()
        return ok({"username": name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class BulkDeleteRequest(BaseModel):
    usernames: List[str]


@router.delete("/users")
def bulk_delete_users(req: BulkDeleteRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if not req.usernames:
        return JSONResponse({"ok": False, "error": "No usernames provided"}, status_code=400)
    with db.bind.connect() as conn:
        for name in req.usernames:
            conn.execute(text("DELETE FROM custom_users WHERE username=:u"), {"u": name})
        _invalidate_cache(conn)
        conn.commit()
    return ok({"deleted": len(req.usernames)})


class AliasRequest(BaseModel):
    names: List[str]
    primary: str


@router.post("/users/alias")
def set_user_alias(req: AliasRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if req.primary not in req.names:
        return JSONResponse({"ok": False, "error": "primary must be one of the selected names"}, status_code=400)
    if len(req.names) < 2:
        return JSONResponse({"ok": False, "error": "Select at least 2 entries to alias"}, status_code=400)

    aliases = [n for n in req.names if n != req.primary]

    with db.bind.connect() as conn:
        existing_aliases: set[str] = set()
        for name in req.names:
            row = conn.execute(text("SELECT aliases FROM custom_users WHERE username=:u"), {"u": name}).fetchone()
            if row and row[0]:
                for a in row[0].split(","):
                    a = a.strip()
                    if a and a != req.primary and a not in req.names:
                        existing_aliases.add(a)

        all_aliases = list(existing_aliases) + aliases
        final_aliases_str = ",".join(all_aliases)

        conn.execute(text(
            "INSERT INTO custom_users (username, aliases, created_at) VALUES (:u, :a, datetime('now')) "
            "ON CONFLICT(username) DO UPDATE SET aliases=:a"
        ), {"u": req.primary, "a": final_aliases_str})
        for alias in aliases:
            conn.execute(text("DELETE FROM custom_users WHERE username=:u"), {"u": alias})
        _invalidate_cache(conn)
        conn.commit()

    return ok({"primary": req.primary, "aliases": all_aliases})


# ── Candidates (populated by sync) ────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(db: Session = Depends(get_db)):
    with db.bind.connect() as conn:
        rows = conn.execute(text(
            "SELECT username, created_at FROM user_candidates ORDER BY username"
        )).fetchall()
    return ok({
        "items": [{"username": r[0], "created_at": r[1]} for r in rows],
        "total": len(rows),
    })


class PromoteRequest(BaseModel):
    usernames: List[str]


@router.post("/candidates/promote")
def promote_candidates(req: PromoteRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if not req.usernames:
        return JSONResponse({"ok": False, "error": "No usernames provided"}, status_code=400)
    with db.bind.connect() as conn:
        for name in req.usernames:
            conn.execute(text(
                "INSERT OR IGNORE INTO custom_users (username, aliases, created_at) VALUES (:u, '', datetime('now'))"
            ), {"u": name})
            conn.execute(text("DELETE FROM user_candidates WHERE username=:u"), {"u": name})
        _invalidate_cache(conn)
        conn.commit()
    return ok({"promoted": len(req.usernames)})


@router.delete("/candidates")
def dismiss_candidates(req: BulkDeleteRequest, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    if not req.usernames:
        return JSONResponse({"ok": False, "error": "No usernames provided"}, status_code=400)
    with db.bind.connect() as conn:
        for name in req.usernames:
            conn.execute(text("DELETE FROM user_candidates WHERE username=:u"), {"u": name})
        conn.commit()
    return ok({"dismissed": len(req.usernames)})


# ── Sync ──────────────────────────────────────────────────────────────────────

@router.post("/users/sync")
def sync_users_from_scans(request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    """Collect directory names from scan data + non-system /etc/passwd users into candidates table."""
    import pwd
    from app.models import Mount
    from app.services.analytics import _latest_scan_id_for_mount

    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    names: set[str] = set()

    with db.bind.connect() as conn:
        # ── Scan-based directory names ────────────────────────────────────────
        for m in mounts:
            sid = _latest_scan_id_for_mount(db, m.id)
            if sid is None:
                continue
            off = len(m.mount_point.rstrip('/')) + 2

            rows = conn.execute(text(f"""
                SELECT DISTINCT SUBSTR(path, {off}) AS comp
                FROM directories
                WHERE mount_id={m.id} AND scan_id={sid}
                AND SUBSTR(path, {off}) != ''
                AND INSTR(SUBSTR(path, {off}), '/') = 0
            """)).fetchall()

            if rows:
                for (comp,) in rows:
                    if comp and _SAFE.match(comp):
                        names.add(comp)
            else:
                frows = conn.execute(text(f"""
                    SELECT
                        CASE
                            WHEN INSTR(SUBSTR(path, {off}), '/') = 0 THEN SUBSTR(path, {off})
                            ELSE SUBSTR(SUBSTR(path, {off}), 1, INSTR(SUBSTR(path, {off}), '/') - 1)
                        END AS comp
                    FROM files WHERE mount_id={m.id} AND scan_id={sid}
                    GROUP BY comp
                """)).fetchall()
                for (comp,) in frows:
                    if comp and _SAFE.match(comp):
                        names.add(comp)

        # ── Non-system /etc/passwd users ──────────────────────────────────────
        try:
            for pw in pwd.getpwall():
                if pw.pw_uid > _SYSTEM_UID_MAX and pw.pw_name and _SAFE.match(pw.pw_name):
                    names.add(pw.pw_name)
        except Exception:
            pass

        # ── uid-resolved names from files ─────────────────────────────────────
        uid_rows = conn.execute(text(
            "SELECT DISTINCT uid, MAX(username) FROM files WHERE uid IS NOT NULL GROUP BY uid"
        )).fetchall()
        for uid, stored in uid_rows:
            if uid is not None and uid > _SYSTEM_UID_MAX:
                if stored and _SAFE.match(stored):
                    names.add(stored)
                else:
                    try:
                        n = pwd.getpwuid(uid).pw_name
                        if _SAFE.match(n):
                            names.add(n)
                    except (KeyError, TypeError):
                        pass

        # ── Skip names already in confirmed list ──────────────────────────────
        confirmed = {r[0] for r in conn.execute(text("SELECT username FROM custom_users")).fetchall()}
        # Also collect all alias names so we don't re-surface them as candidates
        for row in conn.execute(text("SELECT aliases FROM custom_users WHERE aliases != ''")).fetchall():
            for a in row[0].split(","):
                a = a.strip()
                if a:
                    confirmed.add(a)

        # ── Insert new candidates (skip existing and confirmed) ───────────────
        added = 0
        for name in sorted(names):
            if name in confirmed:
                continue
            result = conn.execute(text(
                "INSERT OR IGNORE INTO user_candidates (username, created_at) VALUES (:u, datetime('now'))"
            ), {"u": name})
            added += result.rowcount
        conn.commit()

    logger.info("Synced %d candidates, added %d new", len(names), added)
    return ok({"candidates": len(names), "added": added})


# ── Database maintenance ──────────────────────────────────────────────────────

@router.get("/db-stats")
def get_db_stats(db: Session = Depends(get_db)):
    """Return DB and WAL file sizes (fast — no table scans)."""
    import os
    from app.config import settings as cfg
    db_path = cfg.DB_PATH
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    wal_size = os.path.getsize(db_path + "-wal") if os.path.exists(db_path + "-wal") else 0
    return ok({"db_bytes": db_size, "wal_bytes": wal_size})


@router.post("/purge-scan-data")
def purge_scan_data(request: Request, db: Session = Depends(get_db),
                    _=Depends(auth.require_admin)):
    """For every mount, keep only the latest completed scan's file records.
    Deletes files, directories, scan_errors, attribution cache for all older scans.
    Scan metadata rows are preserved. Runs in background."""
    import threading
    from app.api.routes_scans import _purge_old_scan_files, _wal_checkpoint

    with db.bind.connect() as conn:
        # Find the latest completed scan per mount
        rows = conn.execute(text("""
            SELECT mount_id, MAX(id) as latest_id
            FROM scans
            WHERE mount_id IS NOT NULL AND status = 'completed'
            GROUP BY mount_id
        """)).fetchall()

    scan_ids = [r[1] for r in rows]

    def _run():
        for sid in scan_ids:
            _purge_old_scan_files(sid)
        _wal_checkpoint("TRUNCATE")

    threading.Thread(target=_run, daemon=True, name="manual-purge").start()
    return ok({"purging": True, "mount_count": len(scan_ids)})


# ── Attribution cache management ──────────────────────────────────────────────

@router.delete("/user-cache")
def clear_user_cache(request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    """Clear all user attribution cache entries."""
    with db.bind.connect() as conn:
        _invalidate_cache(conn)
    return ok({"cleared": True})


@router.post("/user-cache/rebuild")
def rebuild_user_cache(request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    """Clear cache and rebuild in background for all accessible mounts."""
    from app.models import Mount
    from app.services.analytics import prewarm_user_cache
    with db.bind.connect() as conn:
        _invalidate_cache(conn)
    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    mount_ids = [m.id for m in mounts]
    if mount_ids:
        prewarm_user_cache(mount_ids)
    return ok({"rebuilding": True, "mount_count": len(mount_ids)})
