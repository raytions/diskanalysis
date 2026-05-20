import re
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.db import get_db
from app.schemas import ok
from app import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings")

_SAFE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    """Read all app-level settings and current custom_users count."""
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
        conn.commit()
    return ok({"path_user_attribution": req.path_user_attribution})


class UserRequest(BaseModel):
    username: str


@router.get("/users")
def list_custom_users(db: Session = Depends(get_db)):
    with db.bind.connect() as conn:
        rows = conn.execute(text(
            "SELECT username, created_at FROM custom_users ORDER BY username"
        )).fetchall()
    return ok({"items": [{"username": r[0], "created_at": r[1]} for r in rows], "total": len(rows)})


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
            conn.execute(text("INSERT INTO custom_users (username, created_at) VALUES (:u, datetime('now'))"), {"u": name})
            conn.commit()
        return ok({"username": name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.delete("/users/{username}")
def delete_custom_user(username: str, request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    with db.bind.connect() as conn:
        conn.execute(text("DELETE FROM custom_users WHERE username=:u"), {"u": username})
        conn.commit()
    return ok({"deleted": username})


@router.post("/users/sync")
def sync_users_from_scans(request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    """Collect all top-level directory names from latest scan data and add to custom_users."""
    import pwd
    from app.models import Mount
    from app.services.analytics import _latest_scan_id_for_mount

    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    names: set[str] = set()

    with db.bind.connect() as conn:
        for m in mounts:
            sid = _latest_scan_id_for_mount(db, m.id)
            if sid is None:
                continue
            off = len(m.mount_point.rstrip('/')) + 2

            # Top-level dirs from directories table
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
                # Fallback: first component from files table (GROUP BY, fast)
                frows = conn.execute(text(f"""
                    SELECT
                        CASE
                            WHEN INSTR(SUBSTR(path, {off}), '/') = 0 THEN SUBSTR(path, {off})
                            ELSE SUBSTR(SUBSTR(path, {off}), 1, INSTR(SUBSTR(path, {off}), '/') - 1)
                        END AS comp
                    FROM files
                    WHERE mount_id={m.id} AND scan_id={sid}
                    GROUP BY comp
                """)).fetchall()
                for (comp,) in frows:
                    if comp and _SAFE.match(comp):
                        names.add(comp)

        # Also add users resolved from uid
        uid_rows = conn.execute(text(
            "SELECT DISTINCT uid, MAX(username) FROM files WHERE uid IS NOT NULL GROUP BY uid"
        )).fetchall()
        for uid, stored in uid_rows:
            if stored and _SAFE.match(stored):
                names.add(stored)
            elif uid is not None:
                try:
                    name = pwd.getpwuid(uid).pw_name
                    if _SAFE.match(name):
                        names.add(name)
                except (KeyError, TypeError):
                    pass

        # Bulk insert — ignore conflicts
        added = 0
        for name in sorted(names):
            result = conn.execute(text("INSERT OR IGNORE INTO custom_users (username, created_at) VALUES (:u, datetime('now'))"), {"u": name})
            added += result.rowcount
        conn.commit()

    logger.info("Synced %d user candidates, added %d new entries to custom_users", len(names), added)
    return ok({"candidates": len(names), "added": added})
