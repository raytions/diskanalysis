import csv
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Mount
from app.schemas import MountSchema, ok
from app.services import analytics, mounts as mount_svc
from app import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# ── In-process overview cache ─────────────────────────────────────────────────
# Caches: mount_scan_pairs, tab counts, users aggregation.
# TTL=60s so stale data expires quickly after a scan completes.
_OV_CACHE: dict = {}
_OV_TTL = 60


def _oc_get(key: str):
    entry = _OV_CACHE.get(key)
    if entry and time.monotonic() - entry[1] < _OV_TTL:
        return entry[0]
    return None


def _oc_set(key: str, value) -> None:
    _OV_CACHE[key] = (value, time.monotonic())


def invalidate_overview_cache() -> None:
    """Call after a scan completes so the next tab load sees fresh data."""
    _OV_CACHE.clear()


def _batch_latest_scan_ids(db, mount_ids: list[int]) -> dict[int, int]:
    """Return {mount_id: latest_scan_id} using 1 query on the scans table.
    Prefers completed scans, falls back to any finished scan (stopped/interrupted).
    Never queries the files table — that's O(N) over millions of rows."""
    from sqlalchemy import text
    if not mount_ids:
        return {}
    ids_str = ",".join(str(i) for i in mount_ids)
    with db.bind.connect() as conn:
        # One query: rank by preferred status then by id DESC, take top per mount
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
    return {r[0]: r[1] for r in rows}


@router.get("/health")
def health():
    return {"ok": True, "status": "up"}


@router.get("/dashboard/overview-tab")
def dashboard_overview_tab(
    tab: str = "files",
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated data for Global Overview tabs: files, dirs, cold, users, dupes."""
    from sqlalchemy import text as sqlt
    from app.services.analytics import _collect_known_usernames

    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    mount_map = {str(m.id): m.mount_point for m in mounts}

    # Build mount_scan_pairs using batch lookup (2 queries) + 60s in-process cache.
    # Old approach called _latest_scan_id_for_mount per-mount (N×2 queries for N mounts).
    mount_scan_pairs: list[tuple[int, int]] = _oc_get("mount_scan_pairs")
    if mount_scan_pairs is None:
        scan_id_map = _batch_latest_scan_ids(db, [m.id for m in mounts])
        seen: set = set()
        mount_scan_pairs = []
        for m in mounts:
            sid = scan_id_map.get(m.id)
            if sid is not None and (m.id, sid) not in seen:
                mount_scan_pairs.append((m.id, sid))
                seen.add((m.id, sid))
        _oc_set("mount_scan_pairs", mount_scan_pairs)

    if not mount_scan_pairs:
        return ok({"items": [], "total": 0, "limit": limit, "offset": offset, "mount_map": mount_map})

    sids = list({sid for _, sid in mount_scan_pairs})
    sid_in = ",".join(str(s) for s in sids)
    sub_n = offset + limit

    # Stable cache key for this combination of mounts/scans
    pairs_key = ",".join(f"{mid}:{sid}" for mid, sid in sorted(mount_scan_pairs))

    def _union_files(extra_where: str = "") -> str:
        parts = [
            f"SELECT id,path,size_bytes,extension,mtime,atime,is_cold,cold_days_bucket,mount_id FROM ("
            f"SELECT id,path,size_bytes,extension,mtime,atime,is_cold,cold_days_bucket,mount_id "
            f"FROM files WHERE mount_id={mid} AND scan_id={sid} AND is_symlink=0{extra_where} "
            f"ORDER BY size_bytes DESC LIMIT {sub_n})"
            for mid, sid in mount_scan_pairs
        ]
        return " UNION ALL ".join(parts)

    def _union_dirs() -> str:
        parts = [
            f"SELECT id,path,total_size_bytes,file_count,cold_file_count,cold_total_size_bytes,mount_id FROM ("
            f"SELECT id,path,total_size_bytes,file_count,cold_file_count,cold_total_size_bytes,mount_id "
            f"FROM directories WHERE mount_id={mid} AND scan_id={sid} "
            f"ORDER BY total_size_bytes DESC LIMIT {sub_n})"
            for mid, sid in mount_scan_pairs
        ]
        return " UNION ALL ".join(parts)

    def _count_files(extra_where: str = "") -> int:
        count_key = f"cnt:f:{extra_where}:{pairs_key}"
        cached = _oc_get(count_key)
        if cached is not None:
            return cached
        if extra_where == "":
            # Use pre-computed totals from scans table — O(1) vs O(N_files)
            with db.bind.connect() as conn:
                result = conn.execute(sqlt(
                    f"SELECT COALESCE(SUM(total_files),0) FROM scans WHERE id IN ({sid_in})"
                )).fetchone()[0] or 0
        elif extra_where == " AND is_cold=1":
            with db.bind.connect() as conn:
                result = conn.execute(sqlt(
                    f"SELECT COALESCE(SUM(cold_file_count),0) FROM scans WHERE id IN ({sid_in})"
                )).fetchone()[0] or 0
        else:
            # Fallback for any other filter: count from files table
            parts = [
                f"SELECT COUNT(*) AS c FROM files "
                f"WHERE mount_id={mid} AND scan_id={sid} AND is_symlink=0{extra_where}"
                for mid, sid in mount_scan_pairs
            ]
            with db.bind.connect() as conn:
                result = conn.execute(sqlt(
                    f"SELECT SUM(c) FROM ({' UNION ALL '.join(parts)})"
                )).fetchone()[0] or 0
        _oc_set(count_key, result)
        return result

    def _count_dirs() -> int:
        count_key = f"cnt:d:{pairs_key}"
        cached = _oc_get(count_key)
        if cached is not None:
            return cached
        # Use pre-computed totals from scans table — O(1) vs O(N_dirs)
        with db.bind.connect() as conn:
            result = conn.execute(sqlt(
                f"SELECT COALESCE(SUM(total_dirs),0) FROM scans WHERE id IN ({sid_in})"
            )).fetchone()[0] or 0
        _oc_set(count_key, result)
        return result

    if tab == "files":
        union = _union_files()
        total = _count_files()
        with db.bind.connect() as conn:
            rows = conn.execute(sqlt(
                f"SELECT id,path,size_bytes,extension,mtime,atime,is_cold,cold_days_bucket,mount_id "
                f"FROM ({union}) ORDER BY size_bytes DESC LIMIT {limit} OFFSET {offset}"
            )).fetchall()
        items = [{"id": r[0], "path": r[1], "size_bytes": r[2], "extension": r[3],
                  "mtime": r[4], "atime": r[5], "is_cold": r[6], "cold_days_bucket": r[7],
                  "mount_id": r[8]}
                 for r in rows]

    elif tab == "dirs":
        union = _union_dirs()
        total = _count_dirs()
        with db.bind.connect() as conn:
            rows = conn.execute(sqlt(
                f"SELECT id,path,total_size_bytes,file_count,cold_file_count,cold_total_size_bytes,mount_id "
                f"FROM ({union}) ORDER BY total_size_bytes DESC LIMIT {limit} OFFSET {offset}"
            )).fetchall()
        items = [{"id": r[0], "path": r[1], "total_size_bytes": r[2], "file_count": r[3],
                  "cold_file_count": r[4], "cold_total_size_bytes": r[5], "mount_id": r[6]}
                 for r in rows]

    elif tab == "cold":
        union = _union_files(" AND is_cold=1")
        total = _count_files(" AND is_cold=1")
        with db.bind.connect() as conn:
            rows = conn.execute(sqlt(
                f"SELECT id,path,size_bytes,extension,mtime,atime,is_cold,cold_days_bucket,mount_id "
                f"FROM ({union}) WHERE is_cold=1 ORDER BY size_bytes DESC LIMIT {limit} OFFSET {offset}"
            )).fetchall()
        items = [{"id": r[0], "path": r[1], "size_bytes": r[2], "extension": r[3],
                  "mtime": r[4], "atime": r[5], "is_cold": r[6], "cold_days_bucket": r[7],
                  "mount_id": r[8]}
                 for r in rows]

    elif tab == "users":
        from app.services.analytics import get_user_summary, _settings_hash
        from collections import defaultdict
        with db.bind.connect() as conn:
            shash = _settings_hash(conn)
        users_key = f"users:{pairs_key}:{shash}"
        all_users = _oc_get(users_key)
        if all_users is None:
            combined: dict = defaultdict(lambda: {"file_count": 0, "total_size_bytes": 0, "uids": set()})
            missing = []
            if mount_scan_pairs:
                # Single batch query against DB attribution cache — no per-mount Python loop
                pair_filter = " OR ".join(f"(mount_id={mid} AND scan_id={sid})" for mid, sid in mount_scan_pairs)
                with db.bind.connect() as conn:
                    db_rows = conn.execute(sqlt(
                        f"SELECT mount_id, scan_id, username, uids, file_count, total_size_bytes "
                        f"FROM user_attribution_cache WHERE settings_hash=:sh AND ({pair_filter})"
                    ), {"sh": shash}).fetchall()
                cached_pairs = {(r[0], r[1]) for r in db_rows}
                missing = [(mid, sid) for mid, sid in mount_scan_pairs if (mid, sid) not in cached_pairs]
                for row in db_rows:
                    name, uids_str, fc, tsb = row[2], row[3] or "", row[4], row[5]
                    combined[name]["file_count"] += fc
                    combined[name]["total_size_bytes"] += tsb
                    for u in uids_str.split(","):
                        u = u.strip()
                        if u and u != "—":
                            combined[name]["uids"].add(u)
            if missing:
                # Compute attribution for uncached mounts in background,
                # then clear key so next request re-fetches the now-complete DB cache.
                def _bg_fill_users(key: str, pairs: list) -> None:
                    from app.db import SessionLocal
                    _db = SessionLocal()
                    try:
                        for _mid, _sid in pairs:
                            try:
                                get_user_summary(_db, _mid, limit=500, scan_id=_sid)
                            except Exception:
                                pass
                    finally:
                        _db.close()
                    _OV_CACHE.pop(key, None)
                import threading
                threading.Thread(target=_bg_fill_users, args=(users_key, missing),
                                 daemon=True, name="users-bg-fill").start()
            all_users = sorted(
                [{"username": n, "file_count": v["file_count"], "total_size_bytes": v["total_size_bytes"],
                  "uids": ",".join(sorted(v["uids"], key=lambda x: int(x) if x.isdigit() else 0))}
                 for n, v in combined.items()],
                key=lambda x: -x["total_size_bytes"],
            )
            _oc_set(users_key, all_users)
        total = len(all_users)
        items = all_users[offset: offset + limit]

    elif tab == "dupes":
        # basename() UDF forces a full-table scan — run the whole query in background.
        # First request returns empty+total=-1 immediately; subsequent requests serve from cache.
        dup_sql = f"""
            SELECT basename(path) AS fname, size_bytes, COUNT(*) AS cnt,
                   SUM(size_bytes) AS wasted, GROUP_CONCAT(path, '|||') AS paths
            FROM files
            WHERE scan_id IN ({sid_in}) AND is_symlink=0 AND size_bytes >= 1048576
            GROUP BY fname, size_bytes HAVING cnt > 1
            ORDER BY wasted DESC LIMIT 1000 OFFSET 0
        """
        dupes_key = f"data:dupes:{sid_in}"
        cached_dupes = _oc_get(dupes_key)
        if cached_dupes is None:
            cached_dupes = {"items": [], "total": -1}
            _oc_set(dupes_key, cached_dupes)  # mark pending so parallel requests skip re-launch

            def _bg_dupes(key: str, sql: str) -> None:
                from app.db import SessionLocal
                _db = SessionLocal()
                try:
                    with _db.bind.connect() as _conn:
                        rows = _conn.execute(sqlt(sql)).fetchall()
                    result = [
                        {"filename": r[0], "size_bytes": r[1], "count": r[2], "wasted_bytes": r[3],
                         "paths": r[4].split("|||") if r[4] else []}
                        for r in rows
                    ]
                    _oc_set(key, {"items": result, "total": len(result)})
                except Exception:
                    _OV_CACHE.pop(key, None)
                finally:
                    _db.close()

            import threading
            threading.Thread(target=_bg_dupes, args=(dupes_key, dup_sql),
                             daemon=True, name="dupes-bg").start()

        all_items = cached_dupes["items"]
        total = cached_dupes["total"]
        items = all_items[offset: offset + limit]

    else:
        return ok({"items": [], "total": 0, "limit": limit, "offset": offset, "mount_map": mount_map})

    return ok({"items": items, "total": total, "limit": limit, "offset": offset, "mount_map": mount_map})


@router.get("/mounts")
def list_mounts(
    fs_type: str | None = None,
    is_network: bool | None = None,
    sort_by: str = "usage_percent",
    order: str = "desc",
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Mount)
    if fs_type:
        q = q.filter(Mount.fs_type == fs_type.lower())
    if is_network is not None:
        q = q.filter(Mount.is_network_fs == is_network)

    sort_col = getattr(Mount, sort_by, Mount.usage_percent)
    if order == "asc":
        q = q.order_by(sort_col.asc())
    else:
        q = q.order_by(sort_col.desc())

    total = q.count()
    items = q.offset(offset).limit(limit).all()
    return ok({"total": total, "limit": limit, "offset": offset, "items": [MountSchema.model_validate(m).model_dump() for m in items]})


class SSHMountRequest(BaseModel):
    username: str
    host: str
    remote_path: str = "/"
    local_path: str
    port: int = 22
    password: str = ""
    key_path: str = ""
    create_local_dir: bool = True
    use_sudo_mkdir: bool = False


def _sudo_mode_enabled(db: Session) -> bool:
    from app.api.routes_settings import get_sudo_mode_enabled
    with db.bind.connect() as conn:
        return get_sudo_mode_enabled(conn)


def _create_mount_dir(path: str, use_sudo: bool, db: Session) -> tuple[bool, str, str]:
    """Create a mount directory. Sudo mode only allows this one whitelisted command."""
    if not os.path.isabs(path) or "\x00" in path or path.rstrip("/") == "":
        return False, "Mount directory must be an absolute path.", ""
    try:
        os.makedirs(path, exist_ok=True)
        return True, "created", f"mkdir -p {path}"
    except OSError as e:
        if not use_sudo:
            return False, f"Cannot create {path}: {e}", f"mkdir -p {path}"
        if not _sudo_mode_enabled(db):
            return False, "Sudo mode is disabled in Settings.", f"sudo install -d -m 0755 -o {os.getuid()} -g {os.getgid()} {path}"
        sudo = shutil.which("sudo")
        install = shutil.which("install")
        if not sudo or not install:
            return False, "sudo or install is not available on this server.", ""
        cmd = [
            sudo, "-n", install, "-d",
            "-m", "0755",
            "-o", str(os.getuid()),
            "-g", str(os.getgid()),
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        cmd_display = " ".join(cmd)
        if result.returncode == 0:
            return True, "created_with_sudo", cmd_display
        err = (result.stderr or result.stdout or "sudo mkdir failed").strip()
        return False, err, cmd_display


def _path_health(path: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Probe path in a subprocess so broken FUSE mounts cannot hang the API worker."""
    probe = (
        "import os,sys\n"
        "p=sys.argv[1]\n"
        "try:\n"
        "    os.stat(p)\n"
        "    os.listdir(p)\n"
        "    print('ok')\n"
        "except OSError as e:\n"
        "    print(f'errno={e.errno} {e.strerror}')\n"
        "    sys.exit(2)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Path health check timed out after {timeout:.0f}s"
    if result.returncode == 0:
        return True, "ok"
    return False, (result.stdout or result.stderr or "Path health check failed").strip()


def _wait_unmounted_and_healthy(path: str, timeout: float = 6.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_msg = ""
    while time.monotonic() < deadline:
        fs_type = _check_proc_mounts(path)
        healthy, health_msg = _path_health(path, timeout=1.0)
        if not fs_type and (healthy or "errno=2" in health_msg):
            return True, "ready"
        last_msg = f"fs_type={fs_type or 'none'}, health={health_msg}"
        time.sleep(0.35)
    return False, last_msg or "mount point did not become ready"


def _diagnose_remote_path(req: SSHMountRequest) -> str:
    ssh = shutil.which("ssh")
    if not ssh:
        return "ssh is not installed"
    host = req.host
    cmd = [
        ssh,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
    ]
    if req.port and req.port != 22:
        cmd += ["-p", str(req.port)]
    if req.key_path:
        cmd += ["-i", req.key_path]
    env = os.environ.copy()
    if req.password:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            return "remote diagnostic skipped because sshpass is not installed"
        env["SSHPASS"] = req.password
        cmd = [sshpass, "-e"] + cmd
    else:
        cmd += ["-o", "BatchMode=yes"]
    remote = shlex.quote(req.remote_path or "/")
    remote_cmd = f"test -d {remote} && test -r {remote} && echo REMOTE_PATH_OK || echo REMOTE_PATH_NOT_READABLE"
    cmd += [f"{req.username}@{host}", remote_cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12, env=env)
    except subprocess.TimeoutExpired:
        return "remote diagnostic timed out"
    output = (result.stdout or result.stderr or "").strip()
    return f"remote diagnostic rc={result.returncode}: {output[:500]}"


@router.post("/mounts/mount-ssh")
def mount_ssh_fs(req: SSHMountRequest, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """Attempt to mount a remote SSH path via sshfs. Returns the sshfs command regardless."""
    from app.services.mount_events import log_mount_event

    local = req.local_path.rstrip("/") or "/"
    host = f"[{req.host}]" if ":" in req.host else req.host
    remote = f"{req.username}@{host}:{req.remote_path}"

    # Build the sshfs command for display
    opts = ["-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=15", "-o", "reconnect"]
    if req.port and req.port != 22:
        opts += ["-p", str(req.port)]
    if req.key_path:
        opts += ["-o", f"IdentityFile={req.key_path}"]
    cmd_parts = ["sshfs"] + opts + [remote, local]
    cmd_display = " ".join(cmd_parts)

    # Already mounted? A stale FUSE mount can remain in /proc/mounts while every
    # access returns EIO, so verify real access before reporting success.
    existing_fs = _check_proc_mounts(local)
    if existing_fs:
        healthy, health_msg = _path_health(local)
        if healthy:
            log_mount_event(
                db, mount_point=local, event_type="mount", status="ok",
                message="SSHFS mount already exists and is accessible.", command=cmd_display,
                details={"remote": remote, "already": True, "fs_type": existing_fs},
            )
            return ok({"mounted": True, "already": True, "fs_type": existing_fs, "command": cmd_display})
        unmounted, unmount_msg = _try_unmount(local, lazy=True)
        log_mount_event(
            db, mount_point=local, event_type="unmount",
            status="ok" if unmounted else "failed",
            message=f"Stale SSHFS mount detected ({health_msg}); lazy unmount: {unmount_msg}",
            command=cmd_display,
            details={"remote": remote, "fs_type": existing_fs},
        )
        if not unmounted:
            return ok({
                "mounted": False,
                "error": f"Existing SSHFS mount is stale ({health_msg}) and could not be unmounted: {unmount_msg}",
                "command": cmd_display,
            })
        ready, ready_msg = _wait_unmounted_and_healthy(local)
        if not ready:
            msg = f"Stale SSHFS mount was detached, but the mount point is still not ready: {ready_msg}"
            log_mount_event(
                db, mount_point=local, event_type="mount", status="failed",
                message=msg, command=cmd_display,
                details={"remote": remote, "unmount": unmount_msg},
            )
            return ok({"mounted": False, "error": msg, "command": cmd_display})

    # Create local directory
    mkdir_command = ""
    if req.create_local_dir:
        created, mkdir_msg, mkdir_command = _create_mount_dir(local, req.use_sudo_mkdir, db)
        log_mount_event(
            db, mount_point=local, event_type="mkdir",
            status="ok" if created else "failed",
            message=mkdir_msg, command=mkdir_command,
            details={"sudo": req.use_sudo_mkdir},
        )
        if not created:
            return ok({"mounted": False, "error": mkdir_msg, "command": cmd_display, "mkdir_command": mkdir_command})
    elif not os.path.isdir(local):
        msg = f"Mount directory does not exist: {local}"
        log_mount_event(db, mount_point=local, event_type="mkdir", status="failed", message=msg)
        return ok({"mounted": False, "error": msg, "command": cmd_display})

    # sshfs not installed
    if not shutil.which("sshfs"):
        log_mount_event(db, mount_point=local, event_type="mount", status="failed",
                        message="sshfs is not installed on this server.", command=cmd_display,
                        details={"remote": remote})
        return ok({
            "mounted": False,
            "error": "sshfs is not installed on this server",
            "command": f"sudo apt install sshfs  # install first, then:\n{cmd_display}",
        })

    env = None
    full_cmd = cmd_parts
    input_text = None
    if req.password:
        cmd_parts = ["sshfs"] + opts + ["-o", "password_stdin"] + [remote, local]
        cmd_display = " ".join(cmd_parts)
        full_cmd = cmd_parts
        input_text = req.password + "\n"

    try:
        result = subprocess.run(full_cmd, input=input_text, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode == 0:
            mounted_fs = None
            health_msg = ""
            for _ in range(10):
                mounted_fs = _check_proc_mounts(local)
                if mounted_fs:
                    healthy, health_msg = _path_health(local, timeout=2.0)
                    if healthy:
                        break
                    mounted_fs = None
                else:
                    health_msg = "not present in /proc/mounts"
                time.sleep(0.3)
            if not mounted_fs:
                remote_diag = _diagnose_remote_path(req)
                msg = "sshfs exited successfully, but the mount point is not accessible: " + health_msg
                cleanup_ok, cleanup_msg = _try_unmount(local, lazy=True)
                log_mount_event(db, mount_point=local, event_type="mount", status="failed",
                                message=f"{msg}; cleanup: {cleanup_msg}", command=cmd_display,
                                details={
                                    "remote": remote,
                                    "mkdir_command": mkdir_command,
                                    "cleanup_ok": cleanup_ok,
                                    "cleanup": cleanup_msg,
                                    "remote_diagnostic": remote_diag,
                                })
                msg += f" Remote check: {remote_diag}."
                if cleanup_ok:
                    msg += f" The bad sshfs mount was detached automatically ({cleanup_msg})."
                else:
                    msg += f" Automatic cleanup failed: {cleanup_msg}."
                return ok({
                    "mounted": False,
                    "error": msg,
                    "command": cmd_display,
                    "mkdir_command": mkdir_command,
                })
            log_mount_event(db, mount_point=local, event_type="mount", status="ok",
                            message="SSHFS mounted successfully.", command=cmd_display,
                            details={"remote": remote, "fs_type": mounted_fs, "mkdir_command": mkdir_command})
            return ok({"mounted": True, "fs_type": mounted_fs, "command": cmd_display, "mkdir_command": mkdir_command})
        err_msg = (result.stderr or result.stdout or "sshfs exited with error").strip()
        remote_diag = _diagnose_remote_path(req)
        cleanup_msg = ""
        mounted_after_failure = _check_proc_mounts(local)
        if mounted_after_failure:
            cleanup_ok, cleanup_msg = _try_unmount(local, lazy=True)
            cleanup_msg = f"; cleanup: {cleanup_msg if cleanup_ok else 'failed: ' + cleanup_msg}"
        full_err = f"{err_msg}. Remote check: {remote_diag}{cleanup_msg}"
        log_mount_event(db, mount_point=local, event_type="mount", status="failed",
                        message=full_err, command=cmd_display,
                        details={
                            "remote": remote,
                            "returncode": result.returncode,
                            "mkdir_command": mkdir_command,
                            "remote_diagnostic": remote_diag,
                            "cleanup": cleanup_msg,
                        })
        return ok({"mounted": False, "error": full_err, "command": cmd_display})
    except subprocess.TimeoutExpired:
        log_mount_event(db, mount_point=local, event_type="mount", status="failed",
                        message="Connection timed out.", command=cmd_display,
                        details={"remote": remote, "mkdir_command": mkdir_command})
        return ok({"mounted": False, "error": "Connection timed out (30s). Check host/port.", "command": cmd_display})
    except Exception as e:
        log_mount_event(db, mount_point=local, event_type="mount", status="failed",
                        message=str(e), command=cmd_display,
                        details={"remote": remote, "mkdir_command": mkdir_command})
        return ok({"mounted": False, "error": str(e), "command": cmd_display})


class MountAddRequest(BaseModel):
    mount_point: str          # local path where it's accessible
    device: str = ""          # source (e.g. 192.168.1.100:/export, //server/share)
    fs_type: str = "nfs4"
    is_network_fs: bool = True
    description: str = ""


def _check_proc_mounts(path: str) -> str | None:
    """Return the fs_type if path is mounted in /proc/mounts, else None."""
    norm = path.rstrip("/") or "/"
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and _decode_proc_mount_field(parts[1]) == norm:
                    return parts[2]
    except OSError:
        pass
    return None


def _decode_proc_mount_field(value: str) -> str:
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value


@router.get("/mounts/test-path")
def test_mount_path(path: str, db: Session = Depends(get_db)):
    """Test whether a local path is accessible and return disk stats."""
    fs_type_in_proc = _check_proc_mounts(path)
    is_mounted = fs_type_in_proc is not None
    health_ok = True
    health_msg = ""
    if is_mounted:
        health_ok, health_msg = _path_health(path)
    accessible = health_ok and os.path.isdir(path)
    result = {
        "path": path, "accessible": accessible, "is_mounted": is_mounted,
        "fs_type": fs_type_in_proc, "total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
    }
    if accessible:
        try:
            usage = shutil.disk_usage(path)
            result.update({"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free})
        except OSError:
            result["accessible"] = False
    elif is_mounted:
        result["stale"] = True
        result["warn"] = health_msg or "Mounted in /proc/mounts but not accessible."
    return ok(result)


@router.get("/mounts/events")
def mount_events(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    from app.services.mount_events import list_mount_events
    return ok({"items": list_mount_events(db, limit=limit)})


@router.get("/mounts/{mount_id}/events")
def mount_events_for_mount(
    mount_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    from app.services.mount_events import list_mount_events
    return ok({"items": list_mount_events(db, mount_id=mount_id, limit=limit)})


@router.post("/mounts/refresh")
def refresh_mounts(request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    updated = mount_svc.refresh_mounts(db)
    return ok({"refreshed": len(updated)})


@router.post("/mounts")
def add_mount(req: MountAddRequest, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """Manually register a mount point (NFS, SMB, SSH, local path)."""
    try:
        path = req.mount_point.rstrip("/") or "/"
        existing = db.query(Mount).filter(Mount.mount_point == path).first()
        if existing:
            return JSONResponse({"ok": False, "error": "Mount point already exists"}, status_code=409)

        accessible = os.path.isdir(path)
        total_bytes = used_bytes = free_bytes = 0
        if accessible:
            try:
                usage = shutil.disk_usage(path)
                total_bytes = usage.total
                used_bytes = usage.used
                free_bytes = usage.free
            except OSError:
                accessible = False

        network_fs_types = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "samba", "fuse.sshfs", "davfs", "glusterfs", "cephfs", "lustre", "fuse.s3fs"}
        is_net = req.is_network_fs or req.fs_type.lower() in network_fs_types

        mount = Mount(
            mount_point=path,
            device=req.device or path,
            fs_type=req.fs_type or "unknown",
            is_network_fs=is_net,
            total_bytes=total_bytes,
            used_bytes=used_bytes,
            free_bytes=free_bytes,
            usage_percent=round(used_bytes / total_bytes * 100, 2) if total_bytes else 0.0,
            is_accessible=accessible,
        )
        db.add(mount)
        db.commit()
        db.refresh(mount)
        from app.services.mount_events import log_mount_event
        log_mount_event(
            db,
            mount_id=mount.id,
            mount_point=mount.mount_point,
            event_type="register",
            status="ok",
            message="Mount registered from Web UI.",
            details={"fs_type": mount.fs_type, "device": mount.device, "accessible": accessible},
        )
        return ok(MountSchema.model_validate(mount).model_dump())
    except Exception as e:
        logger.exception("Error adding mount %s: %s", req.mount_point, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _try_unmount(mount_point: str, lazy: bool = False) -> tuple[bool, str]:
    """Best-effort unmount. Returns (success, message). Never raises."""
    try:
        for tool in filter(None, [shutil.which("fusermount3"), shutil.which("fusermount")]):
            args = [tool, "-u"]
            if lazy:
                args.append("-z")
            args.append(mount_point)
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, "fusermount lazy" if lazy else "fusermount"
        umount = shutil.which("umount")
        if umount:
            args = [umount]
            if lazy:
                args.append("-l")
            args.append(mount_point)
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, "umount lazy" if lazy else "umount"
            return False, (r.stderr or r.stdout or "umount failed").strip()
        return False, "No unmount tool found"
    except Exception as e:
        return False, str(e)


@router.post("/mounts/{mount_id}/unmount")
def unmount_fs(mount_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """Attempt to unmount a filesystem via fusermount or umount."""
    from app.services.mount_events import log_mount_event

    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    fs_type = _check_proc_mounts(m.mount_point)
    if not fs_type:
        log_mount_event(db, mount_id=m.id, mount_point=m.mount_point, event_type="unmount",
                        status="warning", message="Unmount requested but mount point is not currently mounted.")
        return ok({"unmounted": False, "error": "Not currently mounted"})
    healthy, health_msg = _path_health(m.mount_point, timeout=2.0)
    lazy = "fuse" in fs_type.lower() and not healthy
    success, msg = _try_unmount(m.mount_point, lazy=lazy)
    if success:
        m.is_accessible = False
        db.commit()
    log_mount_event(
        db,
        mount_id=m.id,
        mount_point=m.mount_point,
        event_type="unmount",
        status="ok" if success else "failed",
        message=msg if healthy else f"{msg}; stale health check before unmount: {health_msg}",
        details={"fs_type": fs_type, "lazy": lazy, "health": health_msg},
    )
    return ok({"unmounted": success, "method": msg if success else None,
               "error": None if success else msg})


class MountPatchRequest(BaseModel):
    label: str | None = None


@router.patch("/mounts/{mount_id}")
def patch_mount(mount_id: int, req: MountPatchRequest, request: Request,
                db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    if req.label is not None:
        m.label = req.label.strip()
    db.commit()
    return ok(MountSchema.model_validate(m).model_dump())


@router.delete("/mounts/{mount_id}")
def delete_mount(mount_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    from sqlalchemy import text
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    # Best-effort unmount for FUSE filesystems
    fs_type = _check_proc_mounts(m.mount_point)
    if fs_type and "fuse" in fs_type.lower():
        _try_unmount(m.mount_point)
    # Delete mount + scans immediately (fast), then purge files/dirs in background.
    # Doing it synchronously risks an HTTP timeout for large mounts (millions of rows).
    with db.bind.connect() as conn:
        scan_rows = conn.execute(text("SELECT id FROM scans WHERE mount_id = :mid"), {"mid": mount_id}).fetchall()
        scan_ids = [r[0] for r in scan_rows]
        if scan_ids:
            conn.execute(text(f"DELETE FROM scan_errors WHERE scan_id IN ({','.join(str(i) for i in scan_ids)})"))
            conn.execute(text("DELETE FROM scans WHERE mount_id = :mid"), {"mid": mount_id})
        conn.execute(text("DELETE FROM mounts WHERE id = :mid"), {"mid": mount_id})
        conn.commit()
    if scan_ids:
        def _bg_purge(ids: list[int]) -> None:
            from app.db import SessionLocal
            _db = SessionLocal()
            try:
                id_str = ",".join(str(i) for i in ids)
                with _db.bind.connect() as conn:
                    conn.execute(text(f"DELETE FROM directories WHERE scan_id IN ({id_str})"))
                    conn.execute(text(f"DELETE FROM files WHERE scan_id IN ({id_str})"))
                    conn.commit()
                logger.info("Background purge done for scan_ids %s", ids)
            except Exception as e:
                logger.warning("Background purge failed: %s", e)
            finally:
                _db.close()
        threading.Thread(target=_bg_purge, args=(scan_ids,), daemon=True, name=f"purge-{mount_id}").start()
    return ok({"deleted": mount_id})


@router.get("/mounts/{mount_id}")
def get_mount(mount_id: int, db: Session = Depends(get_db)):
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    return ok(MountSchema.model_validate(m).model_dump())


@router.get("/mounts/{mount_id}/largest-files")
def largest_files(
    mount_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
    sort_by: str = "size_bytes",
    sort_dir: str = "desc",
    scan_id: int | None = None,
    count: bool = True,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    data = analytics.get_largest_files(db, mount_id, limit, offset, scan_id, search=search, sort_by=sort_by, sort_dir=sort_dir, count=count)
    return ok(data)


@router.get("/mounts/{mount_id}/largest-directories")
def largest_directories(
    mount_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
    sort_by: str = "total_size_bytes",
    sort_dir: str = "desc",
    scan_id: int | None = None,
    count: bool = True,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    data = analytics.get_largest_directories(db, mount_id, limit, offset, scan_id, search=search, sort_by=sort_by, sort_dir=sort_dir, count=count)
    return ok(data)


@router.get("/mounts/{mount_id}/cold-files")
def cold_files(
    mount_id: int,
    days: int = Query(default=90, ge=1),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
    sort_by: str = "size_bytes",
    sort_dir: str = "desc",
    path_prefix: str | None = None,
    scan_id: int | None = None,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    data = analytics.get_cold_files(db, mount_id, days, limit, offset, path_prefix, scan_id, search=search, sort_by=sort_by, sort_dir=sort_dir)
    return ok(data)


@router.get("/mounts/{mount_id}/cold-directories")
def cold_directories(
    mount_id: int,
    days: int = Query(default=90, ge=1),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    scan_id: int | None = None,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    data = analytics.get_cold_directories(db, mount_id, days, limit, offset, scan_id)
    return ok(data)


@router.get("/mounts/{mount_id}/extension-summary")
def extension_summary(
    mount_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    scan_id: int | None = None,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    data = analytics.get_extension_summary(db, mount_id, limit, scan_id)
    return ok(data)


@router.get("/mounts/{mount_id}/user-summary")
def user_summary(
    mount_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    scan_id: int | None = None,
    db: Session = Depends(get_db),
):
    _assert_mount(db, mount_id)
    try:
        data = analytics.get_user_summary(db, mount_id, limit, scan_id)
        return ok(data)
    except Exception as e:
        logger.exception("user-summary error mount %d: %s", mount_id, e)
        return JSONResponse({"ok": False, "error": "Attribution query failed — " + str(e)[:300]}, status_code=500)


@router.get("/mounts/{mount_id}/scans")
def mount_scans(
    mount_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List scans that include data for this mount (including all_mounts scans)."""
    from app.models import Scan, File as FileModel
    from sqlalchemy import exists
    _assert_mount(db, mount_id)
    scans = (
        db.query(Scan)
        .filter(
            (Scan.mount_id == mount_id) |
            ((Scan.mount_id == None) & exists().where(
                (FileModel.scan_id == Scan.id) & (FileModel.mount_id == mount_id)
            ))
        )
        .order_by(Scan.started_at.desc())
        .limit(limit)
        .all()
    )
    from app.schemas import ScanSchema
    return ok({"items": [ScanSchema.model_validate(s).model_dump() for s in scans]})


# --- exports ---

@router.get("/exports/mounts/{mount_id}.json")
def export_mount_json(mount_id: int, db: Session = Depends(get_db)):
    _assert_mount(db, mount_id)
    files = analytics.get_largest_files(db, mount_id, limit=10000)
    dirs = analytics.get_largest_directories(db, mount_id, limit=10000)
    payload = json.dumps({"files": files, "directories": dirs}, default=str)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=mount_{mount_id}.json"},
    )


@router.get("/exports/mounts/{mount_id}.csv")
def export_mount_csv(mount_id: int, db: Session = Depends(get_db)):
    _assert_mount(db, mount_id)
    files = analytics.get_largest_files(db, mount_id, limit=10000)

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["path", "size_bytes", "extension", "atime", "mtime", "is_cold", "cold_days_bucket"])
        writer.writeheader()
        for f in files:
            writer.writerow({k: f.get(k, "") for k in writer.fieldnames})
        yield buf.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mount_{mount_id}.csv"},
    )


def _assert_mount(db: Session, mount_id: int) -> Mount:
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    return m
