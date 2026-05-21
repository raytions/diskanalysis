import csv
import io
import json
import logging
import os
import shutil
import subprocess
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
    from app.services.analytics import _latest_scan_id_for_mount, _collect_known_usernames

    mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    mount_map = {str(m.id): m.mount_point for m in mounts}

    # Build (mount_id, scan_id) pairs — preserves per-mount granularity for UNION ALL
    mount_scan_pairs: list[tuple[int, int]] = []
    seen: set = set()
    for m in mounts:
        sid = _latest_scan_id_for_mount(db, m.id)
        if sid is not None and (m.id, sid) not in seen:
            mount_scan_pairs.append((m.id, sid))
            seen.add((m.id, sid))

    if not mount_scan_pairs:
        return ok({"items": [], "total": 0, "limit": limit, "offset": offset, "mount_map": mount_map})

    sids = list({sid for _, sid in mount_scan_pairs})
    sid_in = ",".join(str(s) for s in sids)
    sub_n = offset + limit  # rows each mount sub-query contributes

    def _union_files(extra_where: str = "") -> str:
        """UNION ALL of per-mount file queries using ix_files_ms_size covering index.
        Each part is wrapped in SELECT…FROM(subquery) so ORDER BY LIMIT is valid inside UNION ALL."""
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
        parts = [
            f"SELECT COUNT(*) AS c FROM files "
            f"WHERE mount_id={mid} AND scan_id={sid} AND is_symlink=0{extra_where}"
            for mid, sid in mount_scan_pairs
        ]
        with db.bind.connect() as conn:
            return conn.execute(sqlt(
                f"SELECT SUM(c) FROM ({' UNION ALL '.join(parts)})"
            )).fetchone()[0] or 0

    def _count_dirs() -> int:
        parts = [
            f"SELECT COUNT(*) AS c FROM directories WHERE mount_id={mid} AND scan_id={sid}"
            for mid, sid in mount_scan_pairs
        ]
        with db.bind.connect() as conn:
            return conn.execute(sqlt(
                f"SELECT SUM(c) FROM ({' UNION ALL '.join(parts)})"
            )).fetchone()[0] or 0

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
        from app.services.analytics import _build_user_agg_per_mount, get_path_attribution_enabled
        # Collect known usernames (local passwd + custom_users) and check toggle
        with db.bind.connect() as conn:
            path_attr = get_path_attribution_enabled(conn)
            known_usernames, uid_to_canonical, name_to_canonical = _collect_known_usernames(conn, f"scan_id IN ({sid_in})")
        mount_triples = [(mid, sid, mount_map[str(mid)]) for mid, sid in mount_scan_pairs]
        with db.bind.connect() as conn:
            agg = _build_user_agg_per_mount(conn, mount_triples, known_usernames, uid_to_canonical,
                                            path_attribution=path_attr, name_to_canonical=name_to_canonical)
        all_users = sorted(
            [{"username": n, "file_count": v["file_count"], "total_size_bytes": v["total_size_bytes"],
              "uids": ",".join(sorted(v["uids"], key=lambda x: int(x) if x.isdigit() else 0))}
             for n, v in agg.items()],
            key=lambda x: -x["total_size_bytes"],
        )
        total = len(all_users)
        items = all_users[offset: offset + limit]

    elif tab == "dupes":
        dup_sql = f"""
            SELECT basename(path) AS fname, size_bytes, COUNT(*) AS cnt,
                   SUM(size_bytes) AS wasted, GROUP_CONCAT(path, '|||') AS paths
            FROM files
            WHERE scan_id IN ({sid_in}) AND is_symlink=0 AND size_bytes >= 1048576
            GROUP BY fname, size_bytes HAVING cnt > 1
            ORDER BY wasted DESC LIMIT :limit OFFSET :offset
        """
        count_sql = f"""
            SELECT COUNT(*) FROM (
              SELECT 1 FROM files WHERE scan_id IN ({sid_in}) AND is_symlink=0 AND size_bytes >= 1048576
              GROUP BY basename(path), size_bytes HAVING COUNT(*) > 1
            )
        """
        with db.bind.connect() as conn:
            raw_rows = conn.execute(sqlt(dup_sql), {"limit": limit, "offset": offset}).fetchall()
            total = conn.execute(sqlt(count_sql)).fetchone()[0]
        items = [{"filename": r[0], "size_bytes": r[1], "count": r[2], "wasted_bytes": r[3],
                  "paths": r[4].split("|||") if r[4] else []}
                 for r in raw_rows]

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


@router.post("/mounts/mount-ssh")
def mount_ssh_fs(req: SSHMountRequest, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """Attempt to mount a remote SSH path via sshfs. Returns the sshfs command regardless."""
    local = req.local_path.rstrip("/") or "/"
    remote = f"{req.username}@{req.host}:{req.remote_path}"

    # Build the sshfs command for display
    opts = ["-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=15", "-o", "reconnect"]
    if req.port and req.port != 22:
        opts += ["-p", str(req.port)]
    if req.key_path:
        opts += ["-o", f"IdentityFile={req.key_path}"]
    cmd_parts = ["sshfs"] + opts + [remote, local]
    cmd_display = " ".join(cmd_parts)

    # Already mounted?
    if os.path.ismount(local):
        return ok({"mounted": True, "already": True, "command": cmd_display})

    # Create local directory
    try:
        os.makedirs(local, exist_ok=True)
    except OSError as e:
        return ok({"mounted": False, "error": f"Cannot create {local}: {e}", "command": cmd_display})

    # sshfs not installed
    if not shutil.which("sshfs"):
        return ok({
            "mounted": False,
            "error": "sshfs is not installed on this server",
            "command": f"sudo apt install sshfs  # install first, then:\n{cmd_display}",
        })

    # Password auth needs sshpass
    env = None
    full_cmd = cmd_parts
    if req.password:
        if shutil.which("sshpass"):
            env = os.environ.copy()
            env["SSHPASS"] = req.password
            full_cmd = ["sshpass", "-e"] + cmd_parts
        else:
            return ok({
                "mounted": False,
                "error": "sshpass is not installed (required for password auth). Install it or use SSH key auth instead.",
                "command": f"sudo apt install sshpass  # then retry, or use SSH key:\n{cmd_display}",
            })

    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode == 0:
            return ok({"mounted": True, "command": cmd_display})
        err_msg = (result.stderr or result.stdout or "sshfs exited with error").strip()
        return ok({"mounted": False, "error": err_msg, "command": cmd_display})
    except subprocess.TimeoutExpired:
        return ok({"mounted": False, "error": "Connection timed out (30s). Check host/port.", "command": cmd_display})
    except Exception as e:
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
                if len(parts) >= 3 and parts[1] == norm:
                    return parts[2]
    except OSError:
        pass
    return None


@router.get("/mounts/test-path")
def test_mount_path(path: str, db: Session = Depends(get_db)):
    """Test whether a local path is accessible and return disk stats."""
    fs_type_in_proc = _check_proc_mounts(path)
    is_mounted = fs_type_in_proc is not None
    accessible = os.path.isdir(path)
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
        # Mounted (visible in /proc/mounts) but not readable by this process
        # — typically a root-mounted FUSE with no allow_other
        result["accessible"] = True
        result["warn"] = "Mounted but not readable by the web server process. If mounted as root, re-mount as the same user as the web server, or add -o allow_other to the sshfs options."
    return ok(result)


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
        return ok(MountSchema.model_validate(mount).model_dump())
    except Exception as e:
        logger.exception("Error adding mount %s: %s", req.mount_point, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _try_unmount(mount_point: str) -> tuple[bool, str]:
    """Best-effort unmount. Returns (success, message). Never raises."""
    try:
        for tool in filter(None, [shutil.which("fusermount3"), shutil.which("fusermount")]):
            r = subprocess.run([tool, "-u", mount_point], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, "fusermount"
        umount = shutil.which("umount")
        if umount:
            r = subprocess.run([umount, mount_point], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, "umount"
            return False, (r.stderr or r.stdout or "umount failed").strip()
        return False, "No unmount tool found"
    except Exception as e:
        return False, str(e)


@router.post("/mounts/{mount_id}/unmount")
def unmount_fs(mount_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """Attempt to unmount a filesystem via fusermount or umount."""
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    fs_type = _check_proc_mounts(m.mount_point)
    if not fs_type:
        return ok({"unmounted": False, "error": "Not currently mounted"})
    success, msg = _try_unmount(m.mount_point)
    return ok({"unmounted": success, "method": msg if success else None,
               "error": None if success else msg})


@router.delete("/mounts/{mount_id}")
def delete_mount(mount_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    from sqlalchemy import text
    m = db.query(Mount).get(mount_id)
    if not m:
        raise HTTPException(status_code=404, detail="Mount not found")
    # Best-effort unmount before removing from DB (only if it's a FUSE user-space mount)
    fs_type = _check_proc_mounts(m.mount_point)
    if fs_type and "fuse" in fs_type.lower():
        _try_unmount(m.mount_point)
    # Raw SQL to avoid cascade-loading millions of File ORM objects
    with db.bind.connect() as conn:
        rows = conn.execute(text("SELECT id FROM scans WHERE mount_id = :mid"), {"mid": mount_id}).fetchall()
        if rows:
            id_list = ",".join(str(r[0]) for r in rows)
            conn.execute(text(f"DELETE FROM files WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM directories WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM scan_errors WHERE scan_id IN ({id_list})"))
            conn.execute(text("DELETE FROM scans WHERE mount_id = :mid"), {"mid": mount_id})
        conn.execute(text("DELETE FROM mounts WHERE id = :mid"), {"mid": mount_id})
        conn.commit()
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
