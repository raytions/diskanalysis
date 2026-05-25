import base64
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import Mount, Scan, ScanError
from app.schemas import ScanCreateRequest, ScanSchema, ScanErrorSchema, ok
from app import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Maps scan_id → running subprocess.Popen (in-memory; cleared on server restart)
_active_procs: dict[int, subprocess.Popen] = {}


@router.get("/scans")
def list_scans(
    status: str | None = None,
    mount_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Scan)
    if status:
        q = q.filter(Scan.status == status)
    if mount_id is not None:
        q = q.filter(Scan.mount_id == mount_id)
    total = q.count()
    items = q.order_by(Scan.started_at.desc()).offset(offset).limit(limit).all()
    return ok({"total": total, "limit": limit, "offset": offset,
               "items": [ScanSchema.model_validate(s).model_dump() for s in items]})


@router.get("/scans/{scan_id}")
def get_scan(scan_id: int, db: Session = Depends(get_db)):
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    return ok(ScanSchema.model_validate(s).model_dump())


@router.get("/scans/{scan_id}/errors")
def scan_errors(
    scan_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    q = db.query(ScanError).filter(ScanError.scan_id == scan_id)
    total = q.count()
    items = q.offset(offset).limit(limit).all()
    return ok({"total": total, "items": [ScanErrorSchema.model_validate(e).model_dump() for e in items]})


@router.delete("/scans/{scan_id}")
def delete_scan(scan_id: int, request: Request, db: Session = Depends(get_db),
                _: None = Depends(auth.require_admin)):
    from sqlalchemy import text
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    if s.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running scan")
    with db.bind.connect() as conn:
        conn.execute(text("DELETE FROM files WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM directories WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM scan_errors WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM scans WHERE id = :sid"), {"sid": scan_id})
        conn.commit()
    return ok({"deleted": scan_id})


@router.post("/scans/{scan_id}/stop")
def stop_scan(scan_id: int, request: Request, db: Session = Depends(get_db),
              _: None = Depends(auth.require_admin)):
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    if s.status != "running":
        raise HTTPException(status_code=409, detail="Scan is not running")

    # Set DB flag so the worker subprocess can detect stop via polling
    s.stop_requested = True
    db.commit()

    proc = _active_procs.get(scan_id)
    if proc is not None:
        # Send SIGTERM for graceful stop; escalate to SIGKILL in background
        try:
            proc.send_signal(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        threading.Thread(
            target=_escalate_to_kill,
            args=(scan_id, proc),
            daemon=True,
            name=f"stop-{scan_id}",
        ).start()
    else:
        # No in-memory subprocess (e.g. after server restart — use stored worker_pid)
        pid = s.worker_pid
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                threading.Thread(
                    target=_kill_by_pid_after_timeout,
                    args=(scan_id, pid),
                    daemon=True,
                    name=f"stop-pid-{scan_id}",
                ).start()
            except (ProcessLookupError, OSError):
                # Process already gone — mark stopped immediately
                s = db.query(Scan).get(scan_id)
                if s and s.status == "running":
                    s.status = "stopped"
                    s.finished_at = datetime.now()
                    db.commit()
        else:
            # No PID at all — mark directly
            s = db.query(Scan).get(scan_id)
            if s and s.status == "running":
                s.status = "stopped"
                s.finished_at = datetime.now()
                db.commit()

    return ok({"stopping": scan_id})


def _escalate_to_kill(scan_id: int, proc: subprocess.Popen, timeout: int = 8) -> None:
    """Wait for graceful SIGTERM stop; escalate to SIGKILL if the process hangs."""
    try:
        proc.wait(timeout=timeout)
        logger.info("Scan %d subprocess stopped gracefully (rc=%d)", scan_id, proc.returncode)
    except subprocess.TimeoutExpired:
        logger.warning("Scan %d did not stop within %ds — sending SIGKILL", scan_id, timeout)
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass
    _mark_stopped(scan_id)
    _active_procs.pop(scan_id, None)


def _kill_by_pid_after_timeout(scan_id: int, pid: int, timeout: int = 8) -> None:
    """For scans resumed after server restart (no Popen object): SIGTERM then SIGKILL by PID."""
    import time
    time.sleep(timeout)
    try:
        os.kill(pid, 0)  # still alive?
        os.kill(pid, signal.SIGKILL)
        logger.warning("Scan %d (pid=%d) killed with SIGKILL", scan_id, pid)
    except (ProcessLookupError, OSError):
        pass  # already gone
    _mark_stopped(scan_id)


def _mark_stopped(scan_id: int) -> None:
    db = SessionLocal()
    try:
        s = db.query(Scan).get(scan_id)
        if s and s.status == "running":
            s.status = "stopped"
            s.finished_at = datetime.now()
            db.commit()
            logger.info("Scan %d marked as stopped in DB", scan_id)
    except Exception as e:
        logger.warning("Failed to mark scan %d stopped: %s", scan_id, e)
    finally:
        db.close()


def _wal_checkpoint() -> None:
    """Passive WAL checkpoint: flush as many WAL pages as possible without blocking readers.
    Called after each scan completes to prevent unbounded WAL growth."""
    db = SessionLocal()
    try:
        from sqlalchemy import text
        with db.bind.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
        logger.info("WAL checkpoint completed after scan")
    except Exception as e:
        logger.warning("WAL checkpoint failed: %s", e)
    finally:
        db.close()


@router.delete("/scans")
def delete_scans_batch(ids: list[int], request: Request, db: Session = Depends(get_db),
                       _: None = Depends(auth.require_admin)):
    from sqlalchemy import text
    if not ids:
        raise HTTPException(status_code=400, detail="No scan ids provided")
    running = db.query(Scan.id).filter(Scan.id.in_(ids), Scan.status == "running").all()
    if running:
        raise HTTPException(status_code=409, detail=f"Cannot delete running scans: {[r[0] for r in running]}")
    # Delete scan records immediately so they don't reappear on page refresh.
    # Large associated data (files, dirs, errors) is cleaned up in the background.
    id_list = ",".join(str(i) for i in ids)
    with db.bind.connect() as conn:
        conn.execute(text(f"DELETE FROM scans WHERE id IN ({id_list})"))
        conn.commit()
    threading.Thread(target=_delete_scans_background, args=(ids,), daemon=True).start()
    return ok({"deleted": ids})


def _delete_scans_background(ids: list[int]) -> None:
    from sqlalchemy import text
    db = SessionLocal()
    try:
        id_list = ",".join(str(i) for i in ids)
        with db.bind.connect() as conn:
            conn.execute(text(f"DELETE FROM files WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM directories WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM scan_errors WHERE scan_id IN ({id_list})"))
            conn.commit()
        logger.info("Background data cleanup for scans %s complete", ids)
    except Exception as e:
        logger.exception("Background data cleanup for scans %s failed: %s", ids, e)
    finally:
        db.close()


@router.post("/scans")
def create_scan(req: ScanCreateRequest, request: Request, db: Session = Depends(get_db),
                _: None = Depends(auth.require_admin)):
    """
    Trigger a new scan. Single-mount/path: one subprocess. Multi-mount: one subprocess
    per mount launched in parallel, limited by the max_parallel_scans setting.
    """
    if req.scan_mode in ("all_mounts", "selected_mounts"):
        return _create_parallel_scan(req, db)

    # ── Single subprocess (single_mount or path) ─────────────────────────────
    if req.scan_mode == "single_mount":
        if not req.mount_id:
            raise HTTPException(status_code=400, detail="mount_id required for single_mount mode")
        mount = db.query(Mount).get(req.mount_id)
        if not mount:
            raise HTTPException(status_code=404, detail="Mount not found")
        target_path = mount.mount_point
    else:  # path
        if not req.target_path:
            raise HTTPException(status_code=400, detail="target_path required for path mode")
        target_path = req.target_path

    scan = Scan(
        mount_id=req.mount_id,
        started_at=datetime.now(),
        status="running",
        scan_mode=req.scan_mode,
        target_path=target_path,
        filesystem_boundary_only=req.filesystem_boundary_only,
        exclude_rules=req.exclude_rules,
        cold_days=req.cold_days,
        top_n=req.top_n,
        stop_requested=False,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)

    req_b64 = base64.b64encode(json.dumps(req.model_dump()).encode()).decode()
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.scan_worker", str(scan.id), req_b64],
        env=os.environ.copy(),
        cwd=os.getcwd(),
    )
    scan.worker_pid = proc.pid
    db.commit()
    _active_procs[scan.id] = proc
    logger.info("Launched scan %d as subprocess pid=%d (target=%s)", scan.id, proc.pid, target_path)

    threading.Thread(target=_monitor_proc, args=(scan.id, proc), daemon=True,
                     name=f"monitor-{scan.id}").start()
    return ok(ScanSchema.model_validate(scan).model_dump())


def _create_parallel_scan(req: ScanCreateRequest, db):
    """Launch one subprocess per mount in parallel, limited by max_parallel_scans."""
    from app.services import mounts as mount_svc
    from app.api.routes_settings import get_max_parallel_scans
    from sqlalchemy import text

    mount_svc.refresh_mounts(db)

    if req.scan_mode == "all_mounts":
        mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
    else:
        if not req.mount_ids:
            raise HTTPException(status_code=400, detail="mount_ids required for selected_mounts mode")
        mounts = db.query(Mount).filter(
            Mount.id.in_(req.mount_ids), Mount.is_accessible == True
        ).all()

    if not mounts:
        raise HTTPException(status_code=400, detail="No accessible mounts found")

    with db.bind.connect() as conn:
        max_parallel = get_max_parallel_scans(conn)

    # Create one Scan record per mount
    scan_list = []
    req_b64_list = []
    for mount in mounts:
        mount_req = req.model_copy(update={
            "scan_mode": "single_mount",
            "mount_id": mount.id,
            "mount_ids": None,
            "target_path": mount.mount_point,
        })
        scan = Scan(
            mount_id=mount.id,
            started_at=datetime.now(),
            status="running",
            scan_mode="single_mount",
            target_path=mount.mount_point,
            filesystem_boundary_only=req.filesystem_boundary_only,
            exclude_rules=req.exclude_rules,
            cold_days=req.cold_days,
            top_n=req.top_n,
            stop_requested=False,
        )
        db.add(scan)
        db.flush()
        req_b64_list.append(base64.b64encode(json.dumps(mount_req.model_dump()).encode()).decode())
        scan_list.append(scan)
    db.commit()
    for s in scan_list:
        db.refresh(s)

    scan_infos = [(s.id, rb64) for s, rb64 in zip(scan_list, req_b64_list)]
    threading.Thread(
        target=_launch_parallel_mounts,
        args=(scan_infos, max_parallel),
        daemon=True,
        name="parallel-scan-coordinator",
    ).start()

    logger.info("Queued %d parallel scans (max_parallel=%d): ids=%s",
                len(scan_list), max_parallel, [s.id for s in scan_list])
    return ok({
        "multi": True,
        "count": len(scan_list),
        "scan_ids": [s.id for s in scan_list],
        "scans": [ScanSchema.model_validate(s).model_dump() for s in scan_list],
    })


def _launch_parallel_mounts(scan_infos: list[tuple[int, str]], max_parallel: int) -> None:
    """Launch scan subprocesses with max_parallel concurrency (semaphore-controlled)."""
    sem = threading.Semaphore(max_parallel)

    def run_one(scan_id: int, req_b64: str) -> None:
        sem.acquire()
        try:
            actual_start = datetime.now()
            # Update started_at to actual subprocess start time (not queue creation time).
            # This must happen BEFORE Popen so the worker reads the correct value.
            db_pre = SessionLocal()
            try:
                s = db_pre.get(Scan, scan_id)
                if s and s.stop_requested:
                    s.status = "stopped"
                    s.finished_at = actual_start
                    db_pre.commit()
                    return
                if s:
                    s.started_at = actual_start
                    db_pre.commit()
            finally:
                db_pre.close()

            proc = subprocess.Popen(
                [sys.executable, "-m", "app.scan_worker", str(scan_id), req_b64],
                env=os.environ.copy(),
                cwd=os.getcwd(),
            )
            db_pid = SessionLocal()
            try:
                s = db_pid.get(Scan, scan_id)
                if s:
                    s.worker_pid = proc.pid
                    db_pid.commit()
            finally:
                db_pid.close()

            _active_procs[scan_id] = proc
            logger.info("Launched parallel scan %d (pid=%d)", scan_id, proc.pid)

            proc.wait()
            _active_procs.pop(scan_id, None)
            if proc.returncode == 0:
                from app.api.routes_mounts import invalidate_overview_cache
                invalidate_overview_cache()
                threading.Thread(target=_wal_checkpoint, daemon=True,
                                 name=f"wal-ckpt-{scan_id}").start()
            elif proc.returncode is not None and proc.returncode < 0:
                _mark_stopped(scan_id)
        finally:
            sem.release()

    for scan_id, req_b64 in scan_infos:
        threading.Thread(target=run_one, args=(scan_id, req_b64), daemon=True,
                         name=f"pscan-{scan_id}").start()


def _monitor_proc(scan_id: int, proc: subprocess.Popen) -> None:
    """Wait for the subprocess to exit; fix scan status if it was killed unexpectedly."""
    proc.wait()
    _active_procs.pop(scan_id, None)
    rc = proc.returncode
    logger.info("Scan %d subprocess exited (rc=%d)", scan_id, rc)

    if rc == 0:
        from app.api.routes_mounts import invalidate_overview_cache
        invalidate_overview_cache()
        threading.Thread(target=_wal_checkpoint, daemon=True, name=f"wal-ckpt-{scan_id}").start()
    elif rc is not None and rc < 0:
        # Killed by signal (e.g. SIGKILL from stop endpoint or OOM)
        db = SessionLocal()
        try:
            s = db.query(Scan).get(scan_id)
            if s and s.status == "running":
                s.status = "stopped"
                s.finished_at = datetime.now()
                db.commit()
        except Exception:
            pass
        finally:
            db.close()
