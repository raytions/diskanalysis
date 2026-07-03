from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db import SessionLocal
from app.schemas import ScanCreateRequest

logger = logging.getLogger(__name__)


def start_scan_scheduler(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=_run,
        args=(stop_event,),
        daemon=True,
        name="scan-scheduler",
    )
    thread.start()
    return thread


def _run(stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            _tick()
        except Exception as exc:
            logger.warning("Scan scheduler tick failed: %s", exc)


def _tick() -> None:
    from app.api.routes_scans import create_scan
    from app.api.routes_settings import get_scan_schedule, set_setting

    db = SessionLocal()
    try:
        with db.bind.connect() as conn:
            schedule = get_scan_schedule(conn)
        if not schedule["enabled"]:
            return

        next_run_at = _parse_dt(schedule["next_run_at"])
        now = datetime.now()
        if next_run_at and next_run_at > now:
            return

        active = db.execute(text(
            "SELECT COUNT(*) FROM scans WHERE status IN ('running', 'queued')"
        )).fetchone()[0]
        if active:
            logger.info("Scheduled scan skipped because %d scan(s) are active", active)
            _store_next_run(db, schedule["interval_minutes"])
            return

        req = _build_request(schedule)
        create_scan(req, None, db, None)

        with db.bind.connect() as conn:
            set_setting(conn, "scan_schedule_last_started_at", now.isoformat(timespec="seconds"))
            conn.commit()
        _store_next_run(db, schedule["interval_minutes"])
        logger.info("Scheduled scan started: mode=%s interval=%dm",
                    schedule["scan_mode"], schedule["interval_minutes"])
    finally:
        db.close()


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _store_next_run(db, interval_minutes: int) -> None:
    from app.api.routes_settings import set_setting

    next_run = (datetime.now() + timedelta(minutes=interval_minutes)).isoformat(timespec="seconds")
    with db.bind.connect() as conn:
        set_setting(conn, "scan_schedule_next_run_at", next_run)
        conn.commit()


def _build_request(schedule: dict) -> ScanCreateRequest:
    mode = schedule["scan_mode"]
    if mode == "selected_mounts" and not schedule["mount_ids"]:
        mode = "all_mounts"
    if mode == "path" and not schedule["target_path"]:
        mode = "all_mounts"
    return ScanCreateRequest(
        scan_mode=mode,
        scan_type=schedule["scan_type"],
        mount_ids=schedule["mount_ids"] if mode == "selected_mounts" else None,
        target_path=schedule["target_path"] if mode == "path" else None,
        filesystem_boundary_only=schedule["filesystem_boundary_only"],
        exclude_rules=schedule["exclude_rules"] or None,
        cold_days=schedule["cold_days"],
    )
