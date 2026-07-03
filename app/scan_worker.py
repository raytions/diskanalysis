"""
Scan worker — executed as a subprocess by the web server.

    python3 -m app.scan_worker <scan_id> <request_json_b64>

Running as a subprocess (instead of a thread) means the OS can SIGKILL it when
it blocks in an uninterruptible NFS kernel call (D-state), which threads cannot
survive.
"""
import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [scan-worker pid=%(process)d] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scan_worker")


def main() -> None:
    if len(sys.argv) < 3:
        logger.error("Usage: python3 -m app.scan_worker <scan_id> <request_json_b64>")
        sys.exit(1)

    scan_id = int(sys.argv[1])
    req_data = json.loads(base64.b64decode(sys.argv[2]))

    from app.db import SessionLocal, init_db
    from app.models import Scan, Mount
    from app.schemas import ScanCreateRequest
    from app.services.scanner import run_scan
    from app.services import mounts as mount_svc

    init_db()

    stop_event = threading.Event()

    # ── SIGTERM → graceful stop ───────────────────────────────────────────────
    def _on_sigterm(sig, frame):
        logger.info("SIGTERM received — requesting stop for scan %d", scan_id)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_sigterm)

    # ── Poll DB stop_requested flag (backup when SIGTERM can't reach NFS thread) ─
    def _poll_stop():
        poll_db = SessionLocal()
        try:
            while not stop_event.is_set():
                time.sleep(2)
                try:
                    poll_db.expire_all()
                    s = poll_db.get(Scan, scan_id)
                    if s and s.stop_requested:
                        stop_event.set()
                except Exception:
                    pass
        finally:
            poll_db.close()

    threading.Thread(target=_poll_stop, daemon=True, name="stop-poller").start()

    db = SessionLocal()
    try:
        req = ScanCreateRequest(**req_data)
        scan = db.get(Scan, scan_id)
        if not scan:
            logger.error("Scan %d not found in DB", scan_id)
            return

        if req.scan_mode in ("all_mounts", "selected_mounts"):
            mount_svc.refresh_mounts(db)
            if req.scan_mode == "all_mounts":
                mounts = db.query(Mount).filter(Mount.is_accessible == True).all()
            else:
                mounts = db.query(Mount).filter(
                    Mount.id.in_(req.mount_ids or []), Mount.is_accessible == True
                ).all()

            total_files = total_bytes = total_dirs = error_count = cold_files = cold_bytes = 0
            for mount in mounts:
                if stop_event.is_set():
                    break
                _sub_scan(
                    db, scan, mount.mount_point, req,
                    total_files, total_bytes, total_dirs, error_count,
                    cold_files, cold_bytes,
                    stop_event=stop_event,
                )
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
            root = scan.target_path or req.target_path or "/"
            exclude_paths = (
                [e.strip() for e in req.exclude_rules.split(",") if e.strip()]
                if req.exclude_rules else []
            )
            run_scan(
                db=db, scan=scan, root_path=root,
                one_filesystem=req.filesystem_boundary_only,
                exclude_paths=exclude_paths,
                stop_event=stop_event,
            )
            db.refresh(scan)
            if scan.status == "completed" and scan.mount_id:
                from app.services.analytics import prewarm_user_cache
                prewarm_user_cache([scan.mount_id])

    except Exception as exc:
        logger.exception("Scan %d failed: %s", scan_id, exc)
        try:
            db.expire_all()
            scan = db.get(Scan, scan_id)
            if scan and scan.status == "running":
                scan.status = "failed"
                scan.finished_at = datetime.now()
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        logger.info("Scan worker for scan %d exiting (pid=%d)", scan_id, os.getpid())


def _sub_scan(
    db, scan, root, req,
    cumulative_files, cumulative_bytes, cumulative_dirs, cumulative_errors,
    cumulative_cold_files=0, cumulative_cold_bytes=0,
    stop_event=None,
) -> None:
    from app.services.scanner import run_scan
    exclude_paths = (
        [e.strip() for e in req.exclude_rules.split(",") if e.strip()]
        if req.exclude_rules else []
    )
    scan.target_path = root
    db.commit()
    run_scan(
        db=db, scan=scan, root_path=root,
        one_filesystem=req.filesystem_boundary_only,
        exclude_paths=exclude_paths,
        cumulative_files=cumulative_files,
        cumulative_bytes=cumulative_bytes,
        cumulative_dirs=cumulative_dirs,
        cumulative_errors=cumulative_errors,
        cumulative_cold_files=cumulative_cold_files,
        cumulative_cold_bytes=cumulative_cold_bytes,
        mark_completed=False,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    main()
