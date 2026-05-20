import logging
import sys
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
    """Mark any scans left in 'running' state from a previous process as interrupted."""
    from app.db import SessionLocal
    from app.models import Scan
    from sqlalchemy import func
    from app.models import File
    db = SessionLocal()
    try:
        orphans = db.query(Scan).filter(Scan.status == "running").all()
        if not orphans:
            return
        for scan in orphans:
            actual_files = db.query(func.count(File.id)).filter(File.scan_id == scan.id).scalar() or 0
            actual_bytes = db.query(func.sum(File.size_bytes)).filter(File.scan_id == scan.id).scalar() or 0
            scan.status = "interrupted"
            scan.total_files = actual_files
            scan.total_bytes = actual_bytes
            scan.finished_at = datetime.utcnow()
            logger.warning(
                "Marked orphaned scan %d as interrupted (%d files, %.1f GB)",
                scan.id, actual_files, actual_bytes / 1e9,
            )
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting DiskAnalysis — db: %s", settings.DB_PATH)
    init_db()
    _cleanup_orphaned_scans()
    yield
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
