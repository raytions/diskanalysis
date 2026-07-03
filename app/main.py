import logging
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _cleanup_orphaned_scans():
    """Mark 'running' scans as interrupted if their worker subprocess is no longer alive.
    Skips COUNT/SUM queries — with a large DB+WAL these are extremely slow at startup."""
    import os
    from app.db import SessionLocal
    from app.models import Scan
    db = SessionLocal()
    try:
        orphans = db.query(Scan).filter(Scan.status.in_(["running", "queued"])).all()
        if not orphans:
            return
        for scan in orphans:
            if scan.status == "queued":
                scan.status = "interrupted"
                scan.finished_at = datetime.now()
                logger.warning("Marked queued scan %d as interrupted on startup", scan.id)
                continue
            if scan.worker_pid:
                try:
                    os.kill(scan.worker_pid, 0)
                    logger.info("Scan %d worker pid=%d still alive — skipping cleanup", scan.id, scan.worker_pid)
                    continue
                except (ProcessLookupError, OSError):
                    pass
            scan.status = "interrupted"
            scan.finished_at = datetime.now()
            logger.warning("Marked orphaned scan %d as interrupted", scan.id)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting DiskAnalysis — db: %s", settings.DB_PATH)
    init_db()
    _cleanup_orphaned_scans()
    scheduler_stop = threading.Event()
    app.state.scan_scheduler_stop = scheduler_stop
    from app.services.scan_scheduler import start_scan_scheduler
    start_scan_scheduler(scheduler_stop)
    yield
    scheduler_stop.set()
    logger.info("Shutting down")


app = FastAPI(title="DiskAnalysis", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

from app.api.routes_mounts import router as mounts_router
from app.api.routes_scans import router as scans_router
from app.api.routes_settings import router as settings_router
from app.web.routes_pages import router as pages_router

app.include_router(mounts_router)
app.include_router(scans_router)
app.include_router(settings_router)
app.include_router(pages_router)
