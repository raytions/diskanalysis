from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import routes_scans
from app.db import Base
from app.models import Scan


def _session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _scan(status: str, **kw) -> Scan:
    values = {
        "scan_mode": "single_mount",
        "status": status,
        "target_path": "/mnt/test",
        "started_at": datetime.now() - timedelta(seconds=5),
        "filesystem_boundary_only": True,
        "cold_days": 90,
        "top_n": 100,
        "stop_requested": False,
    }
    values.update(kw)
    return Scan(**values)


def test_stop_queued_scan_finishes_immediately():
    Session = _session_factory()
    db = Session()
    try:
        scan = _scan("queued")
        db.add(scan)
        db.commit()

        result = routes_scans._request_scan_stop(scan.id, db)

        db.refresh(scan)
        assert result["state"] == "stopped"
        assert scan.status == "stopped"
        assert scan.stop_requested is True
        assert scan.finished_at is not None
    finally:
        db.close()


def test_stop_running_scan_without_process_marks_stopped():
    Session = _session_factory()
    db = Session()
    routes_scans._active_procs.clear()
    try:
        scan = _scan("running", worker_pid=None)
        db.add(scan)
        db.commit()

        result = routes_scans._request_scan_stop(scan.id, db)

        db.refresh(scan)
        assert result["state"] == "stopped"
        assert scan.status == "stopped"
        assert scan.worker_pid is None
        assert scan.duration_seconds is not None
    finally:
        db.close()


def test_stopped_scan_exit_is_not_treated_as_completed(monkeypatch):
    Session = _session_factory()
    db = Session()
    monkeypatch.setattr(routes_scans, "SessionLocal", Session)
    try:
        scan = _scan("stopped", stop_requested=True, finished_at=datetime.now())
        db.add(scan)
        db.commit()

        assert routes_scans._finalize_scan_process_exit(scan.id, 0) is False

        db.refresh(scan)
        assert scan.status == "stopped"
    finally:
        db.close()
