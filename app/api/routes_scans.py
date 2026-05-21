import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import Mount, Scan, ScanError
from app.schemas import ScanCreateRequest, ScanSchema, ScanErrorSchema, ok
from app.services import mounts as mount_svc
from app.services.scanner import run_scan
from app import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Maps scan_id → Event; set the event to signal the scanner to stop
_stop_events: dict[int, threading.Event] = {}


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
    return ok({"total": total, "limit": limit, "offset": offset, "items": [ScanSchema.model_validate(s).model_dump() for s in items]})


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
def delete_scan(scan_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    from sqlalchemy import text
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    if s.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running scan")
    # Use raw SQL to avoid loading millions of File/Directory ORM objects into memory
    with db.bind.connect() as conn:
        conn.execute(text("DELETE FROM files WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM directories WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM scan_errors WHERE scan_id = :sid"), {"sid": scan_id})
        conn.execute(text("DELETE FROM scans WHERE id = :sid"), {"sid": scan_id})
        conn.commit()
    return ok({"deleted": scan_id})


@router.post("/scans/{scan_id}/stop")
def stop_scan(scan_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    s = db.query(Scan).get(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scan not found")
    if s.status != "running":
        raise HTTPException(status_code=409, detail="Scan is not running")
    event = _stop_events.get(scan_id)
    if event:
        event.set()
    return ok({"stopping": scan_id})


@router.delete("/scans")
def delete_scans_batch(ids: list[int], request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    if not ids:
        raise HTTPException(status_code=400, detail="No scan ids provided")
    running = db.query(Scan.id).filter(Scan.id.in_(ids), Scan.status == "running").all()
    if running:
        raise HTTPException(status_code=409, detail=f"Cannot delete running scans: {[r[0] for r in running]}")
    # Return immediately; actual deletion runs in background to avoid freezing the UI
    threading.Thread(target=_delete_scans_background, args=(ids,), daemon=True).start()
    return ok({"deleting": ids})


def _delete_scans_background(ids: list[int]) -> None:
    from sqlalchemy import text
    db = SessionLocal()
    try:
        id_list = ",".join(str(i) for i in ids)
        with db.bind.connect() as conn:
            conn.execute(text(f"DELETE FROM files WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM directories WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM scan_errors WHERE scan_id IN ({id_list})"))
            conn.execute(text(f"DELETE FROM scans WHERE id IN ({id_list})"))
            conn.commit()
        logger.info("Background deletion of scans %s complete", ids)
    except Exception as e:
        logger.exception("Background deletion of scans %s failed: %s", ids, e)
    finally:
        db.close()


@router.post("/scans")
def create_scan(req: ScanCreateRequest, request: Request, db: Session = Depends(get_db), _: None = Depends(auth.require_admin)):
    """
    Trigger a new scan. Runs in a background thread so the HTTP response
    returns immediately. Poll GET /api/scans/{id} to check status.
    """
    # Resolve target path(s)
    if req.scan_mode == "single_mount":
        if not req.mount_id:
            raise HTTPException(status_code=400, detail="mount_id required for single_mount mode")
        mount = db.query(Mount).get(req.mount_id)
        if not mount:
            raise HTTPException(status_code=404, detail="Mount not found")
        target_path = mount.mount_point
    elif req.scan_mode == "path":
        if not req.target_path:
            raise HTTPException(status_code=400, detail="target_path required for path mode")
        target_path = req.target_path
    elif req.scan_mode == "selected_mounts":
        if not req.mount_ids:
            raise HTTPException(status_code=400, detail="mount_ids required for selected_mounts mode")
        target_path = None
    else:
        target_path = None  # all_mounts

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
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    scan_id = scan.id
    stop_event = threading.Event()
    _stop_events[scan_id] = stop_event

    # Launch scan in background thread
    t = threading.Thread(
        target=_run_scan_background,
        args=(scan_id, req, stop_event),
        daemon=True,
    )
    t.start()

    return ok(ScanSchema.model_validate(scan).model_dump())


def _run_scan_background(scan_id: int, req: ScanCreateRequest, stop_event: threading.Event) -> None:
    """Execute the scan in a fresh DB session (runs in a separate thread)."""
    db = SessionLocal()
    try:
        scan = db.query(Scan).get(scan_id)
        if not scan:
            return

        if req.scan_mode in ("all_mounts", "selected_mounts"):
            # Refresh mount list, then scan each target mount
            mount_svc.refresh_mounts(db)
            if req.scan_mode == "all_mounts":
                mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
            else:
                mounts = db.query(Mount).filter(Mount.id.in_(req.mount_ids), Mount.is_accessible == True).all()
            scan.status = "running"
            db.commit()

            total_files = total_bytes = total_dirs = error_count = 0
            cold_files = cold_bytes = 0
            for mount in mounts:
                if stop_event.is_set():
                    break
                _sub_scan(db, scan, mount.mount_point, req, total_files, total_bytes, total_dirs, error_count, cold_files, cold_bytes,
                          since_timestamp=_get_incremental_baseline(db, req, mount.mount_point),
                          stop_event=stop_event)
                db.refresh(scan)
                if scan.status == "stopped":
                    break
                total_files = scan.total_files
                total_bytes = scan.total_bytes
                total_dirs = scan.total_dirs
                error_count = scan.error_count
                cold_files = scan.cold_file_count
                cold_bytes = scan.cold_total_size_bytes

            if scan.status not in ("stopped", "failed"):
                scan.status = "completed"
                scan.finished_at = datetime.now()
                scan.duration_seconds = (scan.finished_at - scan.started_at).total_seconds()
                db.commit()
                from app.services.analytics import prewarm_user_cache
                prewarm_user_cache([m.id for m in mounts])
        else:
            # single_mount or path mode — use scan.target_path (set by create_scan)
            root = scan.target_path or req.target_path or "/"
            exclude_paths = [e.strip() for e in req.exclude_rules.split(",") if e.strip()] if req.exclude_rules else []
            since_ts = _get_incremental_baseline(db, req, root)
            run_scan(
                db=db,
                scan=scan,
                root_path=root,
                one_filesystem=req.filesystem_boundary_only,
                exclude_paths=exclude_paths,
                since_timestamp=since_ts,
                stop_event=stop_event,
            )
            db.refresh(scan)
            if scan.status == "completed" and scan.mount_id:
                from app.services.analytics import prewarm_user_cache
                prewarm_user_cache([scan.mount_id])
    except Exception as exc:
        logger.exception("Scan %d failed: %s", scan_id, exc)
        try:
            scan = db.query(Scan).get(scan_id)
            if scan:
                scan.status = "failed"
                scan.error_summary = str(exc)
                scan.finished_at = datetime.now()
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        _stop_events.pop(scan_id, None)


def _get_incremental_baseline(db: "Session", req: "ScanCreateRequest", root_path: str) -> float | None:
    """Return the mtime cutoff for incremental scans, or None for full scans."""
    if req.scan_type != "incremental":
        return None
    # Find the last completed scan that covers this path
    last = (
        db.query(Scan)
        .filter(Scan.status == "completed", Scan.target_path == root_path)
        .order_by(Scan.started_at.desc())
        .first()
    )
    if last and last.started_at:
        return last.started_at.timestamp()
    return None


def _sub_scan(db: Session, scan: Scan, root: str, req: ScanCreateRequest, cumulative_files: int, cumulative_bytes: int, cumulative_dirs: int, cumulative_errors: int, cumulative_cold_files: int = 0, cumulative_cold_bytes: int = 0, since_timestamp: float | None = None, stop_event: threading.Event | None = None) -> None:
    exclude_paths = [e.strip() for e in req.exclude_rules.split(",") if e.strip()] if req.exclude_rules else []
    scan.target_path = root
    db.commit()
    run_scan(
        db=db,
        scan=scan,
        root_path=root,
        one_filesystem=req.filesystem_boundary_only,
        exclude_paths=exclude_paths,
        since_timestamp=since_timestamp,
        cumulative_files=cumulative_files,
        cumulative_bytes=cumulative_bytes,
        cumulative_dirs=cumulative_dirs,
        cumulative_errors=cumulative_errors,
        cumulative_cold_files=cumulative_cold_files,
        cumulative_cold_bytes=cumulative_cold_bytes,
        mark_completed=False,
        stop_event=stop_event,
    )
